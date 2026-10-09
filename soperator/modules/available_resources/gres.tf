locals {
  gres_by_platforms = tomap({
    (local.platforms.gpu-h100-sxm)   = "nvidia_h100_80gb_hbm3"
    (local.platforms.gpu-h200-sxm)   = "nvidia_h200"
    (local.platforms.gpu-b200-sxm)   = "nvidia_b200"
    (local.platforms.gpu-b200-sxm-a) = "nvidia_b200"
    (local.platforms.gpu-b300-sxm)   = "nvidia_b300_sxm6_ac"
    (local.platforms.gpu-gb300)      = "nvidia_gb300"
  })

  # The list of GPUs should be sorted by Links field to correspond to the GPU order in nvidia-smi
  gres_config_by_platforms = tomap({
    (local.platforms.cpu-e2) = [
      "AutoDetect=off"
    ]
    (local.platforms.cpu-d3) = [
      "AutoDetect=off"
    ]
    (local.platforms.gpu-h100-sxm) = [
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia4 Cores=0-31 Links=-1,1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia5 Cores=0-31 Links=1,-1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia6 Cores=0-31 Links=1,1,-1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia7 Cores=0-31 Links=1,1,1,-1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia0 Cores=32-63 Links=1,1,1,1,-1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia1 Cores=32-63 Links=1,1,1,1,1,-1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia2 Cores=32-63 Links=1,1,1,1,1,1,-1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h100-sxm]} File=/dev/nvidia3 Cores=32-63 Links=1,1,1,1,1,1,1,-1 Flags=nvidia_gpu_env",
    ]
    (local.platforms.gpu-h200-sxm) = [
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia4 Cores=0-31 Links=-1,1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia5 Cores=0-31 Links=1,-1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia6 Cores=0-31 Links=1,1,-1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia7 Cores=0-31 Links=1,1,1,-1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia0 Cores=32-63 Links=1,1,1,1,-1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia1 Cores=32-63 Links=1,1,1,1,1,-1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia2 Cores=32-63 Links=1,1,1,1,1,1,-1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-h200-sxm]} File=/dev/nvidia3 Cores=32-63 Links=1,1,1,1,1,1,1,-1 Flags=nvidia_gpu_env",
    ]
    (local.platforms.gpu-b200-sxm) = [
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia4 Cores=0-39 Links=-1,1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia5 Cores=0-39 Links=1,-1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia6 Cores=0-39 Links=1,1,-1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia7 Cores=0-39 Links=1,1,1,-1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia0 Cores=40-79 Links=1,1,1,1,-1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia1 Cores=40-79 Links=1,1,1,1,1,-1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia2 Cores=40-79 Links=1,1,1,1,1,1,-1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm]} File=/dev/nvidia3 Cores=40-79 Links=1,1,1,1,1,1,1,-1 Flags=nvidia_gpu_env",
    ]
    (local.platforms.gpu-b200-sxm-a) = [
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia7 Cores=0-39 Links=-1,1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia6 Cores=0-39 Links=1,-1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia5 Cores=0-39 Links=1,1,-1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia4 Cores=0-39 Links=1,1,1,-1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia3 Cores=40-79 Links=1,1,1,1,-1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia2 Cores=40-79 Links=1,1,1,1,1,-1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia1 Cores=40-79 Links=1,1,1,1,1,1,-1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b200-sxm-a]} File=/dev/nvidia0 Cores=40-79 Links=1,1,1,1,1,1,1,-1 Flags=nvidia_gpu_env",
    ]
    (local.platforms.gpu-b300-sxm) = [
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia7 Cores=0-47 Links=-1,1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia6 Cores=0-47 Links=1,-1,1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia5 Cores=0-47 Links=1,1,-1,1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia4 Cores=0-47 Links=1,1,1,-1,1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia3 Cores=48-95 Links=1,1,1,1,-1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia2 Cores=48-95 Links=1,1,1,1,1,-1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia1 Cores=48-95 Links=1,1,1,1,1,1,-1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-b300-sxm]} File=/dev/nvidia0 Cores=48-95 Links=1,1,1,1,1,1,1,-1 Flags=nvidia_gpu_env",
    ]
    (local.platforms.gpu-gb300) = [
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-gb300]} File=/dev/nvidia3 Cores=0-55 Links=-1,1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-gb300]} File=/dev/nvidia2 Cores=0-55 Links=1,-1,1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-gb300]} File=/dev/nvidia1 Cores=56-111 Links=1,1,-1,1 Flags=nvidia_gpu_env",
      "AutoDetect=off Name=gpu Type=${local.gres_by_platforms[local.platforms.gpu-gb300]} File=/dev/nvidia0 Cores=56-111 Links=1,1,1,-1 Flags=nvidia_gpu_env",
    ]
  })
}
