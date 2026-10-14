# DSVM 

This Terraform solution deploys a Data Science Virtual Machine (DSVM) is a virtual machine with pre-installed popular libraries for data analytics and machine learning. A DSVM can be used as an environment for training models and experimenting with data.

## The image is based on Ubuntu and includes pre-installed software
* Conda, a package manager with Python 2.7 (environment py27) and Python 3.10 (py310).
* Jupyter Notebook and JupyterLab, tools for interactive and reproducible computations.
* Data analysis, scientific computing and data visualisation libraries: Pandas, NumPy, SciPy, Matplotlib.
* Machine Learning libraries: PyTorch, CatBoost, TensorFlow, scikit-learn, Keras.
* PySpark, a library for interacting with Apache Spark™ and building distributed data processing pipelines.
* NLTK, a suite of natural language processing libraries and data.
* Docker®, a container management system.
* Git, a version control system.
* NVIDIA® Data Center Driver, CUDA® Toolkit 12, and Container Toolkit for accelerating machine learning and other compute-intensive applications on NVIDIA GPUs available in Nebius.ai.
* Optimised libraries and instruments for working with images: scikit-image, opencv-python, Pillow.

## Use cases
* Analysis and prediction of user behavior.
* Analysis of system operation and prediction of failures.
* Customer segmentation.
* Classification of images, documents, and any types of data.
* Recommendation systems.
* Speech synthesis and recognition services.
* Dialog engines.

## Deployment instructions

### Prerequisites

1. Install [Nebius CLI](https://docs.nebius.dev/en/cli/#installation):
   ```bash
   curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
   ```

2. Reload your shell session:

   ```bash
   exec -l $SHELL
   ```

   or

   ```bash
   source ~/.bashrc
   ```

3. [Configure](https://docs.nebius.ai/cli/configure/) Nebius CLI (we recommend using [service account](https://docs.nebius.ai/iam/service-accounts/manage/)):
   ```bash
   nebius init
   ```

### Installation

To deploy the solution, follow these steps:

1. Load environment variables:
   ```bash
   source ./environment.sh
   ```
2. Initialize Terraform:
   ```bash
   terraform init
   ```
3. Replace the placeholder content in `terraform.tfvars` with the configuration values that you need. See the details [below](#configuration-variables).
4. Preview the deployment plan:
   ```bash
   terraform plan
   ```
5. Apply the configuration:
   ```bash
   terraform apply
   ```
   Wait for the operation to complete.

## Configuration variables

Update the following variables in the `terraform.tfvars` file with your own values:

- `parent_id`
- `subnet_id`
- `ssh_user_name`
- `ssh_public_key`

## Usage
* In your web browser, go to ```http://<VM's_IP_address>:8888``` to access the UI. The password is your VM’s ID
* In the UI, open the terminal.
* Activate conda environment: ```conda activate py310```	
* Set a new password and restart the Jupyter Lab daemon:
```
jupyter-lab password
sudo systemctl restart jupyter-lab
```

## Product composition
| Software	                | Version    |
| ------------------------- | ---------- |
| Ubuntu	                   | 22.04 LTS  |
| CatBoost	                | 1.2        |
| Conda	                   | 23.5.0     |
| Docker	                   | 24.0.2     |
| Git	                      | 2.25.1     |
| JypiterLab	             | 3.6.3      |
| Keras	                   | 2.11.0     |
| Matplotlib	             | 3.7.1      |
| NLTK	                   | 3.7        |
| NVIDIA CUDA Toolkit	    | 12.0.1     |
| NVIDIA Container Toolkit  | 1.13.2     |
| NVIDIA Data Center Driver | 535.54.03  |
| NumPy	                   | 1.22.3     |
| Pandas	                   | 1.4.2      |
| Pillow	                   | 9.4.0      |
| PySpark	                | 3.2.1      |
| PyTorch	                | 2.0.1      |
| SciPy	                   | 1.8.1      |
| TensorFlow	             | 2.11.0     |
| opencv-python	          | 4.6.0      |
| scikit-image	             | 0.19.2     |
| scikit-learn	             | 1.1.1      |
| accelerate	             | 0.17.1     |
| datasets	                | 2.9.0      |
| transformers	             | 4.27.1     |
| torchvision	             | 0.15.2     |
