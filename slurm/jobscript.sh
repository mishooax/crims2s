#!/bin/bash
#SBATCH --job-name=test-dask
#SBATCH --qos=nf
#SBATCH --ntasks=8
#SBATCH --cpus-per-task=2
#SBATCH --time=15:00
#SBATCH --mem-per-cpu=8G
#SBATCH --output=test-dask.out
#SBATCH --error=test-dask.err
#SBATCH --chdir=/home/syma/codes/crims2s/daskexp

# export environment variables
export DASK_TEMP_DIR=/ec/res4/scratch/syma
export DASK_LOG_DIR=${PWD}/logs

# run the python script
module load conda
conda activate s2s_repro
python clusters.py --num-workers-per-node 8 --worker-memory-limit 16G --num-threads-per-worker 2

echo '--- DONE! ---'
