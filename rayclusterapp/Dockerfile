FROM rayproject/ray:nightly-py39-cu128

# Install required packages
RUN sudo apt update && \
    sudo apt install -y kmod infiniband-diags ibverbs-utils libibverbs-dev perftest net-tools && \
    sudo rm -rf /var/lib/apt/lists/*

# Add your training script
COPY train.py /app/train.py

WORKDIR /app

# Install PyTorch with CUDA 12.8 support
RUN pip install torch --index-url https://download.pytorch.org/whl/cu128

# The sleep infinity command keeps the container running and make it available for the Ray cluster.
# It keeps the Ray Head and Worker containers alive and ready to receive tasks.
# Make sure to use Ray Operator to manage the Ray cluster lifecycle.
CMD ["sleep", "infinity"]
