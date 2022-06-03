#!/bin/bash
#SBATCH --job-name=test-mpi
#SBATCH --qos=np
#SBATCH --ntasks=2
#SBATCH --cpus-per-task=256
#SBATCH --time=05:00
##SBATCH --mem-per-cpu=100M
#SBATCH --output=test-mpi.%j.out
#SBATCH --error=test-mpi.%j.out
#SBATCH --chdir=/home/syma/codes/crims2s/daskexp

echo "SLURM node list:" $SLURM_JOB_NODELIST
export DASK_TEMP_DIR=/ec/res4/scratch/syma
export DASK_LOG_DIR=${PWD}/logs

echo "Hostnames:" `​scontrol show hostnames`

module load conda
conda activate s2s_repro
echo "Running on ..." $HOSTNAME
python clusters.py --num-workers-per-node 8 --worker-memory-limit 16G --num-threads-per-worker 2

echo '--- DONE! ---'
