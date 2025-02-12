"""Generate a ML-ready dataset from the S2S competition data."""
import os
import pathlib
import warnings
from typing import List

import hydra
import omegaconf
import pandas as pd
import xarray as xr

from dask.distributed import Future, wait

print("Warning: ignoring future warning")
warnings.simplefilter(action="ignore", category=FutureWarning)

# git clone https://github.com/ecmwf-lab/ecmwf-ml-utils.git
# pip install with : pip install --user -e .
from ecmwf_ml_utils.dask_utils import CustomLogger, CustomSshClient, NonDaskClient

from .distribution import fit_gamma_xarray, fit_normal_xarray
from .transform import normalize_dataset
from .util import (
    ECMWF_FORECASTS,
    NCEP_FORECASTS,
    TEST_THRESHOLD,
    add_biweekly_dim,
    fix_dataset_dims,
)

_logger = CustomLogger(__name__)


def obs_of_forecast(forecast, raw_obs, threshold=TEST_THRESHOLD):
    valid_time = forecast.valid_time.compute()

    if threshold:
        threshold_pd = pd.to_datetime(threshold)
        valid_time = forecast.valid_time.where(
            forecast.valid_time < threshold_pd, drop=True
        ).compute()

    obs = raw_obs.sel(time=valid_time)

    return obs


class ExamplePartMaker:
    """Fabricated abstraction that makes part of an example. Abstraction is useful
    because it allows the user to choose what he wants his examples made of. Plausible
    par of examples include features, observations, terciles, etc."""

    def __call__(self, year, example):
        """Generate the part of an example for a given year."""
        raise NotImplementedError


class FeatureExamplePartMaker(ExamplePartMaker):
    def __init__(
        self,
        features,
        weekly_steps=False,
        n_realizations=None,
    ):
        self.features = features
        self.weekly_steps = weekly_steps
        self.n_realizations = n_realizations

    def __call__(self, year, example):
        features = (
            self.features.isel(forecast_monthday=0)
            .sel(forecast_year=year)
            .to_array()
            .rename("features")
            .transpose(
                "lead_time",
                "latitude",
                "longitude",
                "realization",
                "variable",
            )
        )

        if self.n_realizations:
            features = features.isel(realization=list(range(self.n_realizations)))

        if self.weekly_steps:
            features = features.isel(lead_time=list(range(0, 7 * 6, 7)))

        return features


class ModelExamplePartMaker(ExamplePartMaker):
    def __init__(self, model, n_realizations=None):
        self.model = model
        self.n_realizations = n_realizations

    def __call__(self, year, example):
        model = (
            self.model.isel(forecast_monthday=0)
            .sel(forecast_year=year)
            .transpose(
                "lead_time",
                "latitude",
                "longitude",
                "realization",
            )
        )

        if self.n_realizations:
            model = model.isel(realization=list(range(self.n_realizations)))

        return model


class TercilesExamplePartMaker(ExamplePartMaker):
    def __init__(self, terciles):
        self.terciles = terciles

    def __call__(self, year, example):
        model = example["model"]

        return self.terciles.sel(forecast_time=model.forecast_time).transpose(
            "category", "lead_time", "latitude", "longitude"
        )


class ObsExamplePartMaker(ExamplePartMaker):
    def __init__(self, obs, threshold=None):
        self.obs = obs
        self.threshold = threshold

    def __call__(self, year, example):
        model = example["model"]
        return obs_of_forecast(model, self.obs, self.threshold)


class EdgesExamplePartMaker(ExamplePartMaker):
    def __init__(self, edges):
        self.edges = edges

    def __call__(self, year, example):
        model = example["model"]
        month = int(model.forecast_time.dt.month)
        day = int(model.forecast_time.dt.day)

        forecast_idx = ECMWF_FORECASTS.index((month, day))

        return self.edges.isel(week=forecast_idx)


def make_model_params(
    model: xr.Dataset,
    weeks_12=False,
    interp_tp_na=False,
) -> xr.Dataset:
    """Convert daily model data to biweekly aggregated distributions. This
    function outputs the parameters for 4 distributions. Three for precipitation and
    one for temperature."""
    max_valid_time = model.valid_time.compute().max()
    model_biweekly = add_biweekly_dim(model, weeks_12=weeks_12)

    last_lead_times = model_biweekly.valid_time.where(
        model_biweekly.valid_time <= max_valid_time, model_biweekly.forecast_time
    ).argmax(dim="lead_time")

    t2m_parameters = fit_normal_xarray(
        model_biweekly.t2m, dim=["lead_time", "realization"]
    )

    tp_data = model_biweekly.isel(lead_time=last_lead_times).tp
    if interp_tp_na:
        realization_mean = tp_data.mean(dim="realization")
        tp_data = xr.where(~tp_data.isnull(), tp_data, realization_mean)

        if tp_data.isnull().any():
            for max_gap in [1, 5]:
                """Perform the interpolation with increasingly large interpolation windows."""
                tp_data = (
                    tp_data.interpolate_na(dim="realization", max_gap=max_gap)
                    .interpolate_na(
                        dim="longitude", max_gap=max_gap, use_coordinate=False
                    )
                    .interpolate_na(
                        dim="latitude", max_gap=max_gap, use_coordinate=False
                    )
                    .interpolate_na(
                        dim="biweekly_forecast", max_gap=max_gap, use_coordinate=False
                    )
                )

        if tp_data.isnull().any():
            _logger.warning(
                "Interpolation did not get rid of all TP nans. Replacing with zeros."
            )
            tp_data = tp_data.fillna(0.0)

    tp_parameters = fit_gamma_xarray(tp_data, dim="realization")
    tp_parameters_normal = fit_normal_xarray(tp_data, dim="realization")

    tp_cube_root = (tp_data ** (1.0 / 3.0)).rename("tp_cube_root")
    tp_parameters_cube_root = fit_normal_xarray(tp_cube_root, dim="realization")

    merged = xr.merge(
        [
            t2m_parameters,
            tp_parameters,
            tp_parameters_normal,
            tp_parameters_cube_root,
        ]
    )

    return merged


class ModelParametersExamplePartMaker(ExamplePartMaker):
    def __init__(self, weeks_12):
        self.weeks_12 = weeks_12

    def __call__(self, year, example):
        _logger.debug("Computing model distribution parameters...")

        model = example["model"]
        return make_model_params(model)


class ECCCModelParameters(ExamplePartMaker):
    def __init__(self, eccc_data, weeks_12=False):
        self.weeks_12 = weeks_12
        self.eccc_data = eccc_data

    def __call__(self, year, example):
        if year < 1998 or year in [2018, 2019] or year > 2020:
            return self.make_unavailable_eccc()
        else:
            return self.make_eccc_example_part(year)

    def make_unavailable_eccc(self):
        return None

    def make_eccc_example_part(self, year):
        eccc_data = (
            self.eccc_data.isel(forecast_monthday=0)
            .sel(forecast_year=year)
            .transpose("lead_time", "latitude", "longitude", "realization")
        )

        params = make_model_params(eccc_data, weeks_12=self.weeks_12, interp_tp_na=True)

        return params


def trim_ncep_forecast(ncep_forecast: xr.Dataset, reference_forecast):
    """Trim ncep forecast so that its coordinates match a reference forecast."""
    ncep_forecast = ncep_forecast.squeeze(dim="forecast_monthday", drop=False)
    ncep_forecast = ncep_forecast.sel(forecast_year=reference_forecast.forecast_year)

    ncep_forecast = ncep_forecast.where(
        ncep_forecast.valid_time >= reference_forecast.valid_time[0], drop=True
    )

    ncep_forecast = ncep_forecast.assign_coords(
        {
            "lead_time": reference_forecast.lead_time[
                slice(0, ncep_forecast.sizes["lead_time"])
            ],
            "forecast_time": reference_forecast.forecast_time,
            "forecast_monthday": reference_forecast.forecast_monthday,
        }
    )

    return ncep_forecast


class NCEPModelParameters(ExamplePartMaker):
    def __init__(self, ncep_data, weeks_12=False):
        self.ncep_data = ncep_data
        self.weeks_12 = False

    def __call__(self, year, example):
        model = example["model"]
        if (year >= 1999 and year <= 2010) and model.forecast_monthday not in [
            "0102",
            "1231",
        ]:
            return self.make_ncep_example_part(year, example)
        elif year == 2020:
            return self.make_ncep_example_part(year, example)
        else:
            return None

    def make_ncep_example_part(self, year, example):
        model = example["model"]
        ncep_data = trim_ncep_forecast(self.ncep_data, model)

        ncep_data = ncep_data.transpose(
            "lead_time", "latitude", "longitude", "realization"
        )

        params = make_model_params(ncep_data, weeks_12=self.weeks_12, interp_tp_na=True)

        return params


def datestrings_from_input_dir(input_dir, center):
    input_path = pathlib.Path(input_dir)
    print(input_path)
    out = sorted(
        [
            x.stem.split("-")[-1]
            for x in input_path.iterdir()
            if "t2m" in x.stem and center in x.stem
        ]
    )
    assert out, (input_dir, out)
    return out


def preprocess_single_level_file(d):
    level = int(d.plev[0])
    new_names = {k: f"{k}{level}" for k in d.data_vars}

    return fix_dataset_dims(d.rename(new_names).isel(plev=0).drop("plev"))


def read_flat_fields(input_dir, center, fields, datestring, file_label="hindcast"):
    filenames = [
        f"{input_dir}/{center}-{file_label}-{f}-{datestring}.nc" for f in fields
    ]
    _logger.debug("read_flat_fields: opening %s", " ".join(filenames))
    flat_dataset = xr.open_mfdataset(
        filenames, preprocess=fix_dataset_dims, parallel=True, chunks={}
    )

    for dim in ["depth_below_and_layer", "meanSea"]:
        if dim in flat_dataset.dims:
            flat_dataset = flat_dataset.isel({dim: 0})
            flat_dataset = flat_dataset.drop(dim)

    return flat_dataset


def read_plev_fields(input_dir, center, fields, datestring, file_label="hindcast"):
    plev_files = []
    for field, levels in fields.items():
        for level in levels:
            plev_files.append(
                f"{input_dir}/{center}-{file_label}-{field}{level}-{datestring}.nc"
            )
    _logger.debug("read_plev_fields: opening %s", " ".join(plev_files))
    return xr.open_mfdataset(
        plev_files, parallel=True, preprocess=preprocess_single_level_file, chunks={}
    )


def remove_nans(dataset):
    """Little hack to make sure we can train an not have everything explode.
    The data should be approx. zero centered so replacing the nans with zeros should
    not create extreme values. The dataset contains an LSM so the network should figure
    out that it should not use those zeroes that are outside of the land sea mask."""
    return dataset.fillna(0.0)


def read_raw_obs(t2m_file, pr_file, preprocess=lambda x: x):
    t2m = preprocess(xr.open_dataset(t2m_file))
    pr = preprocess(xr.open_dataset(pr_file))

    return xr.merge([t2m, pr])


def ecmwf_datestring_to_ncep_datestring(datestring, set="train"):
    """For an ecmwf datestring, output the corresponding ncep forecast datestring."""

    if set == "test":
        # The datestrings are fine in the test set. It's only in the train set that
        # we have problems.
        return datestring

    year, month, day = int(datestring[:4]), int(datestring[4:6]), int(datestring[6:])
    index = ECMWF_FORECASTS.index((month, day))
    output_year = 2010

    if index in [0, 52]:
        # Some forecasts have no ncep correspondence.
        return None
    else:
        ncep_forecast = NCEP_FORECASTS[index - 1]
        return "{:04}{:02}{:02}".format(output_year, ncep_forecast[0], ncep_forecast[1])


@hydra.main(config_path="conf", config_name="mldataset")
def cli(cfg):
    # https://confluence.ecmwf.int/display/UDOC/HPC2020%3A+Reading+a+NetCDF+or+HDF5+file+gets+stuck
    # os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"
    # get complete stack traces from Hydra
    os.environ["HYDRA_FULL_ERROR"] = "1"

    output_dir = hydra.utils.to_absolute_path(cfg.output_dir)
    output_path = pathlib.Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("config written in ", output_path / "confg.yaml")
    with open(output_path / "confg.yaml", "w") as f:
        cfg_string = omegaconf.OmegaConf.to_yaml(cfg, resolve=True)
        f.write(cfg_string)

    if cfg.dask.enabled:
        client = CustomSshClient(
            num_workers_per_node=cfg.dask.num_workers_per_node,
            worker_memory_limit=cfg.dask.max_memory_per_worker,
            num_threads_per_worker=cfg.dask.num_threads_per_worker,
            # scheduler_port=8786,
            # dashboard_port=8787,
            local_temp_dir=cfg.dask.local_temp_dir,
            dask_log_dir=cfg.dask.log_dir,
        )
    else:
        client = NonDaskClient()

    input_dir = hydra.utils.to_absolute_path(cfg.set.input.flat)
    input_dir_plev = hydra.utils.to_absolute_path(cfg.set.input.plev)

    _logger.info(f"Will output in {output_path}")

    datestrings = datestrings_from_input_dir(input_dir, cfg.center)

    if cfg.index is not None:
        datestrings = datestrings[cfg.index : cfg.index + 1]

    _logger.info(f"Will only operate on datestrings: {datestrings}")

    edges = xr.open_dataset(
        hydra.utils.to_absolute_path(cfg.set.aggregated_obs.edges),
        chunks={},
    )
    _logger.info("edges.chunks: %s", edges.chunks)
    edges = edges.persist()

    # edges =
    # Dimensions:        (week: 53, category_edge: 2, lead_time: 2, latitude: 121,
    #                     longitude: 240)
    # Coordinates:
    #   * latitude       (latitude) float64 90.0 88.5 87.0 85.5 ... -87.0 -88.5 -90.0
    #   * lead_time      (lead_time) timedelta64[ns] 14 days 28 days
    #   * longitude      (longitude) float64 0.0 1.5 3.0 4.5 ... 355.5 357.0 358.5
    #   * category_edge  (category_edge) float64 0.3333 0.6667
    #   * week           (week) int64 1 2 3 4 5 6 7 8 9 ... 45 46 47 48 49 50 51 52 53
    # Data variables:
    #     t2m            (week, category_edge, lead_time, latitude, longitude) float32 ...
    #     tp             (week, category_edge, lead_time, latitude, longitude) float32 ...

    obs_terciled = xr.open_dataset(
        hydra.utils.to_absolute_path(cfg.set.aggregated_obs.terciled),
        chunks={},
    )
    _logger.info("obs_terciled.chunks: %s", obs_terciled.chunks)
    obs_terciled = obs_terciled.persist()

    # obs_terciled =
    # Dimensions:        (category: 3, forecast_time: 1060, latitude: 121,
    #                     lead_time: 2, longitude: 240)
    # Coordinates:
    #   * category       (category) object 'below normal' 'near normal' 'above normal'
    #   * forecast_time  (forecast_time) datetime64[ns] 2000-01-02 ... 2019-12-31
    #   * latitude       (latitude) float64 90.0 88.5 87.0 85.5 ... -87.0 -88.5 -90.0
    #   * lead_time      (lead_time) timedelta64[ns] 14 days 28 days
    #   * longitude      (longitude) float64 0.0 1.5 3.0 4.5 ... 355.5 357.0 358.5
    #     valid_time     (lead_time, forecast_time) datetime64[ns] ...
    # Data variables:
    #     t2m            (category, lead_time, forecast_time, latitude, longitude) float32 ...
    #     tp             (category, lead_time, forecast_time, latitude, longitude) float32 ...

    # raw_obs = read_raw_obs(cfg.raw_obs.t2m_file, cfg.raw_obs.pr_file)

    _logger.debug("------- START PROCESSING... ------")

    futures = []
    for datestring in datestrings:
        single_date_futures: List[Future] = process_single_date(
            client,
            cfg,
            output_path,
            input_dir,
            input_dir_plev,
            edges,
            obs_terciled,
            datestring,
        )
        futures.append(single_date_futures)
    
    _ = wait(futures, return_when="ALL_COMPLETED")

    _logger.debug("---- DONE! ----")



def save_example(example, output_path: pathlib.Path):
    """Save an example to a single netcdf file. x is the input features. obs is the
    observations for every valid time in the forecast. y is the terciled target
    distribution (below, within, above normal)."""
    if isinstance(example["terciles"], Future):
        example["terciles"] = example["terciles"].result()  # wait for Future
    forecast_time = example["terciles"].forecast_time
    year = int(forecast_time.dt.year)
    month = int(forecast_time.dt.month)
    day = int(forecast_time.dt.day)
    filename = f"train_example_{year:04}{month:02}{day:02}.nc"

    output_file = output_path / filename

    if output_file.exists():
        _logger.info(f"Target file {output_file} already exists. Replacing.")
        output_file.unlink()

    for k in example:
        _logger.debug(f"Saving group {k}.")
        mode = "a" if output_file.exists() else "w"
        if isinstance(example[k], Future):
            # we're waiting on the Future to complete b/c some results may be "None", need to test for that
            # TODO: these .result() calls are likely to damage performance
            # is there a better way to do this?
            example[k] = example[k].result()  # wait for Future
        if example[k] is not None:
            example[k].to_netcdf(output_file, group=f"/{k}", mode=mode, compute=True)
    

def read_data_single_date(cfg, input_dir, input_dir_plev, datestring):
    _logger.info("Reading flat fields...")
    flat_dataset = read_flat_fields(
        input_dir,
        cfg.center,
        cfg.fields.flat,
        datestring,
        file_label=cfg.set.file_label,
    )

    _logger.info("Reading fields with vertical levels...")
    plev_fields = cfg.fields.get("plev", dict())
    datasets = [flat_dataset]
    if plev_fields:
        plev_dataset = read_plev_fields(
            input_dir_plev,
            cfg.center,
            plev_fields,
            datestring,
            file_label=cfg.set.file_label,
        )
        datasets.append(plev_dataset)

    features = xr.merge(datasets)
    features = normalize_dataset(features)
    features = remove_nans(features)

    _logger.info("Datestring: %s --- features: %s", datestring, features)

    model = flat_dataset[["t2m", "tp", "forecast_time"]]

    _logger.info("Datestring: %s --- reading ECCC data ...", datestring)
    eccc_data = read_flat_fields(
        input_dir, "eccc", ["t2m", "tp"], datestring, file_label=cfg.set.file_label
    )

    ncep_datestring = ecmwf_datestring_to_ncep_datestring(datestring, set=cfg.set.name)

    if ncep_datestring:
        _logger.info("Datestring: %s --- reading NCEP data ...", ncep_datestring)
        ncep_data = read_flat_fields(
            input_dir,
            "ncep",
            ["t2m", "tp"],
            ncep_datestring,
            file_label=cfg.set.file_label,
        )
        ncep_data = ncep_data.where(
            ncep_data.forecast_year >= model.forecast_year[0], drop=True
        )
    else:
        _logger.info("No corresponding NCEP forecast found. Skipping NCEP.")
        ncep_data = None

    # Super evil temporary hack: the ECMWF data is sprinkled with nans, but it looks
    # like what are nans should be zeros. So we replace them arbitratily with zeros.
    model["tp"] = model["tp"].fillna(0.0)

    # A lot of the fields we use have null lead time 0 for ECMWF.
    # We remove lead time 1 for all the dataset.
    # Shouldn't matter too much since we are interested in leads 14-42 anyways.
    features = features.isel(lead_time=slice(1, None))
    model = model.isel(lead_time=slice(1, None))

    years = model.forecast_year

    if cfg.set.last_forecast:
        years = model.forecast_year[
            model.forecast_time < pd.to_datetime(cfg.set.last_forecast)
        ]

    if cfg.set.year:
        years = years.where(years == cfg.set.year, drop=True)

    _logger.info(f"Will make examples for years: {years.data}")

    return features, model, eccc_data, ncep_data, years


def process_single_date(
    client, cfg, output_path, input_dir, input_dir_plev, edges, obs_terciled, datestring
) -> List[Future]:

    futures: List[Future] = []

    _logger.info(f"Processing datestring {datestring}...")

    features, model, eccc_data, ncep_data, years = client.submit(
        read_data_single_date,
        cfg, input_dir, input_dir_plev, datestring
    ).result()

    part_makers = [
        (
            "features",
            FeatureExamplePartMaker(
                features,
                n_realizations=cfg.n_realizations,
                weekly_steps=True,
            ),
        ),
        ("model", ModelExamplePartMaker(model, n_realizations=cfg.n_realizations)),
        ("terciles", TercilesExamplePartMaker(obs_terciled)),
        # ("obs", ObsExamplePartMaker(raw_obs, cfg.set.valid_threshold)),
        ("edges", EdgesExamplePartMaker(edges)),
        (
            "model_parameters",
            ModelParametersExamplePartMaker(weeks_12=cfg.weeks_12),
        ),
        ("eccc_parameters", ECCCModelParameters(eccc_data, weeks_12=cfg.weeks_12)),
        ("ncep_parameters", NCEPModelParameters(ncep_data, weeks_12=cfg.weeks_12)),
    ]

    for year in years:
        _logger.info(f"Making example for year {year.data}.")
        example = {}
        for name, part_maker in part_makers:
            _logger.debug(f" Making part {name} ...")
            example_part_future = client.submit(part_maker, year, example)
            futures.append(example_part_future)
            # if example_part_future is not None:
            example[name] = example_part_future # part_maker(year, example)

        save_future = client.submit(save_example, example, output_path)
        futures.append(save_future)

    return futures

if __name__ == "__main__":
    cli()
