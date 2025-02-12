from setuptools import setup, find_packages

setup(
    name="crims2s",
    author="CRIM",
    author_email="david.landry@crim.ca",
    install_requires=[
        "dask",
        "hydra-core",
        "hydra-submitit-launcher",
        "pytorch-lightning",
    ],
    entry_points={
        "console_scripts": [
            "s2s_infer = crims2s.training.infer:cli",
            "s2s_mldataset = crims2s.mldataset:cli",
            "s2s_train = crims2s.training.train:cli",
            "s2s_split_plev = crims2s.splitter.splitter:cli",
            "s2s_test_hydra = crims2s.test_hydra:cli",
        ]
    },
    packages=find_packages(exclude="test"),
    include_package_data=True,
)
