# GROMACS for Hopper (sm_90) and Blackwell RTX PRO 6000 (sm_120). The NGC
# nvcr.io/hpc/gromacs:2023.2 image fails on sm_120 with cudaErrorInvalidPtx
# (its CUDA predates Blackwell), so this builds GROMACS from source.
#   docker build -t gromacs:2026.4-cuda12.8-sm90-120 .
#   docker build --build-arg CUDA=13.0.1 -t gromacs:2026.4-cuda13.0-sm90-120 .   (measured identical)
# 2026.x matters for RTX: it fixes the NB-kernel warp-uniform detection (2026.1,
# issue 5546) and PME tuning on a single rank + GPU (2026.1, issue 5578).
# Kernel-level experiments (patched builds) live in spikes/S16-gromacs-rtx-tuning/.
ARG CUDA=12.8.1
FROM nvidia/cuda:${CUDA}-devel-ubuntu24.04 AS build
ARG GMX_VERSION=2026.4
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build wget ca-certificates python3 libfftw3-dev libhwloc-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN wget -q https://ftp.gromacs.org/gromacs/gromacs-${GMX_VERSION}.tar.gz && tar xzf gromacs-${GMX_VERSION}.tar.gz
RUN cmake -S gromacs-${GMX_VERSION} -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/gromacs \
      -DGMX_GPU=CUDA -DGMX_CUDA_TARGET_SM="90;120" \
      -DGMX_SIMD=AVX2_256 -DGMX_MPI=OFF -DGMX_OPENMP=ON -DGMX_HWLOC=ON \
      -DGMX_FFT_LIBRARY=fftw3 -DGMX_BUILD_OWN_FFTW=OFF \
      -DGMX_INSTALL_NBLIB_API=OFF -DGMXAPI=OFF -DREGRESSIONTEST_DOWNLOAD=OFF && \
    cmake --build build -j 64 && cmake --install build

# slim runtime: CUDA base image + only cufft (the runtime image carries cuBLAS etc., 3.5 GB)
FROM nvidia/cuda:${CUDA}-base-ubuntu24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libfftw3-double3 libfftw3-single3 libhwloc15 libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/cuda/lib64/libcufft.so.1* /usr/local/cuda/lib64/
COPY --from=build /opt/gromacs /opt/gromacs
ENV PATH=/opt/gromacs/bin:$PATH GMX_MAXBACKUP=-1
ENTRYPOINT ["/bin/bash", "-c"]
