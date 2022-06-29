#!/bin/bash
#SBATCH --job-name=s2s_mldataset
#SBATCH --qos=nf
#SBATCH --ntasks=8
#SBATCH --cpus-per-task=2
#SBATCH --time=15:00
#SBATCH --mem-per-cpu=8G
#SBATCH --output=s2s_mldataset.out
#SBATCH --error=s2s_mldataset.err
#SBATCH --export=CONDA_PREFIX

if [ -z "$SLURM_JOB_NODELIST" ]
then
    echo "ERROR: This script should be run with sbatch. (but the env var SLURM_JOB_NODELIST is not defined, which means this is not running in a slurm job)."
    exit
fi

env_name=$(basename $CONDA_PREFIX)

module load conda
conda activate $env_name

s2s_mldataset "$@"

echo '--- DONE! ---'
