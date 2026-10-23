"""
Tests for the ReproScore 1.1 fixes (notes/reproscore-review.md in notebook-study).
Each test is a case that 1.0 scored wrongly; the review id is in the test name.
Run: python3 -m pytest tests/test_fixes_1_1.py
"""
import json
import time
from pathlib import Path

import pytest

from src.scoring.rrs import RRSScorer, _SubMetricDetectors as D, _find_readme
from src.scoring.ros import ROSScorer, ExecutionEvidence
from src.scoring.rubric import Rubric, load_rubric


def make(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def nb(*code, outputs=False):
    cells = [{"cell_type": "code", "source": c, "metadata": {}, "execution_count": None,
              "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}] if outputs else []}
             for c in code]
    return json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


# A1 README detection --------------------------------------------------------------
@pytest.mark.parametrize("name", ["readme.md", "Readme.md", "README.markdown", "README.MD"])
def test_a1_readme_any_case(tmp_path, name):
    repo = make(tmp_path, {name: "# X\n## Installation\npip install -r requirements.txt\n"})
    assert _find_readme(repo) is not None
    assert D.doc_structure(repo).raw_score > 0


def test_a1_readme_prefers_markdown(tmp_path):
    repo = make(tmp_path, {"README.txt": "t", "README.md": "m"})
    assert _find_readme(repo).name == "README.md"


# A2 Python version -----------------------------------------------------------------
@pytest.mark.parametrize("files", [
    {"pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8"\n'},
    {"setup.cfg": "[options]\npython_requires = >=3.8\n"},
    {"setup.py": 'setup(name="x", python_requires=">=3.8")\n'},
    {"environment.yml": "dependencies:\n  - python>=3.9\n"},
    {"environment.yml": "dependencies:\n  - python==3.9.7\n"},
    {"Pipfile": '[requires]\npython_version = "3.10"\n'},
    {"binder/runtime.txt": "python-3.10\n"},
    {"binder/environment.yml": "dependencies:\n  - python=3.10\n"},
])
def test_a2_python_version_declared(tmp_path, files):
    assert D.python_version_declared(make(tmp_path, files)).raw_score == 100


def test_a2_unrelated_python_mention_is_not_a_declaration(tmp_path):
    repo = make(tmp_path, {"environment.yml": "dependencies:\n  - python-dateutil\n"})
    assert D.python_version_declared(repo).raw_score == 0


# A3 data pointer ---------------------------------------------------------------------
def test_a3_badges_and_clone_urls_are_not_data_pointers(tmp_path):
    repo = make(tmp_path, {"README.md": "[![DOI](https://zenodo.org/badge/1.svg)](https://doi.org/10.5281/zenodo.1)\n"
                                        "Clone with `git clone https://github.com/o/r`\n"
                                        "Cite the paper: https://doi.org/10.1093/gigascience/giad113\n"})
    assert D.data_pointer(repo).raw_score == 0


def test_a3_data_link_in_data_context_counts(tmp_path):
    repo = make(tmp_path, {"README.md": "# X\n## Data\nThe dataset is archived at https://zenodo.org/records/123\n"})
    assert D.data_pointer(repo).raw_score == 100
    repo2 = make(tmp_path / "b", {"README.md": "Download the data from https://doi.org/10.5281/zenodo.42 first.\n"})
    assert D.data_pointer(repo2).raw_score == 100


def test_a3_github_only_with_data_context(tmp_path):
    repo = make(tmp_path, {"README.md": "The input files are in https://github.com/o/data-repo/releases\n"})
    assert D.data_pointer(repo).raw_score == 60


# A4 seed management -----------------------------------------------------------------
def test_a4_modern_seeding_and_resample(tmp_path):
    repo = make(tmp_path, {"a.py": "import numpy as np\nrng = np.random.default_rng(42)\nx = rng.normal(size=3)\n",
                           "b.py": "import scipy.signal\ny = scipy.signal.resample(x, 10)\n"})
    ev, sigma = D.seed_management(repo)
    assert sigma == 1.0
    assert RRSScorer().score(repo).penalty_seed == 0


@pytest.mark.parametrize("seeded", [
    "rng = np.random.RandomState(0)\nx = rng.rand(3)\n",
    "from sklearn.model_selection import train_test_split\na, b = train_test_split(X, random_state=0)\nx = np.random.rand(3)\n",
    "import torch\ntorch.cuda.manual_seed_all(1)\nx = torch.rand(3)\n",
    "import jax\nkey = jax.random.PRNGKey(0)\nx = jax.random.normal(key, (3,))\n",
])
def test_a4_seeding_forms(tmp_path, seeded):
    _, sigma = D.seed_management(make(tmp_path, {"a.py": "import numpy as np\n" + seeded}))
    assert sigma == 1.0


def test_a4_unseeded_randomness_still_detected(tmp_path):
    repo = make(tmp_path, {"a.py": "import random\nx = random.choice([1, 2])\n",
                           "b.py": "import numpy as np\nrng = np.random.default_rng()\nx = rng.normal()\n"})
    _, sigma = D.seed_management(repo)
    assert sigma == 0.0


# A5 import resolvability -------------------------------------------------------------
def test_a5_stdlib_only_without_dependency_file(tmp_path):
    repo = make(tmp_path, {"a.py": "import os\nimport json\nimport argparse\nimport ctypes\n"})
    assert D.import_resolvability(repo).raw_score == 100


def test_a5_local_modules_are_not_third_party(tmp_path):
    repo = make(tmp_path, {"requirements.txt": "numpy==1.26\n", "functions/__init__.py": "", "functions/api.py": "x=1",
                           "helpers.py": "", "nb.ipynb": nb("import numpy\nfrom functions.api import ApiKey\nimport helpers")})
    assert D.import_resolvability(repo).raw_score == 100


@pytest.mark.parametrize("dep", ["binder/requirements.txt", ".binder/requirements.txt", "requirements/base.txt",
                                 "requirements-notebooks.txt"])
def test_a5_dependency_files_in_usual_places(tmp_path, dep):
    repo = make(tmp_path, {dep: "numpy==1.26\n", "a.py": "import numpy\n"})
    assert D.import_resolvability(repo).raw_score == 100


def test_a5_known_import_names(tmp_path):
    repo = make(tmp_path, {"requirements.txt": "scikit-learn\nPillow\nopencv-python\nbeautifulsoup4\nPyYAML\n",
                           "a.py": "import sklearn\nimport PIL\nimport cv2\nimport bs4\nimport yaml\n"})
    assert D.import_resolvability(repo).raw_score == 100


def test_a5_third_party_without_any_declaration_is_zero(tmp_path):
    assert D.import_resolvability(make(tmp_path, {"a.py": "import numpy\n"})).raw_score == 0


# A6 dependency pinning ---------------------------------------------------------------
def test_a6_conda_exact_pins(tmp_path):
    repo = make(tmp_path, {"environment.yml": "name: x\nchannels:\n  - conda-forge\ndependencies:\n"
                                              "  - python=3.10\n  - numpy=1.26.4\n  - pandas=2.2.2=py310h1\n"
                                              "  - pip:\n    - requests==2.31.0\n"})
    assert D.dep_pinning(repo).raw_score == 80


def test_a6_setup_py_metadata_lines_are_not_packages(tmp_path):
    repo = make(tmp_path, {"setup.py": 'from setuptools import setup\nsetup(name="x", version="1.0",\n'
                                       '      install_requires=["numpy==1.26.4", "pandas==2.2.2"])\n'})
    assert D.dep_pinning(repo).raw_score == 80


def test_a6_pyproject_dependencies(tmp_path):
    repo = make(tmp_path, {"pyproject.toml": '[project]\nname = "x"\nversion = "1"\n'
                                             'dependencies = ["numpy>=1.20", "pandas"]\n'})
    ev = D.dep_pinning(repo)
    assert 50 <= ev.raw_score < 80 and "1/2 constrained" in ev.deduction_reason.replace("0/2 fully pinned; ", "")


def test_a6_requirements_options_and_comments(tmp_path):
    repo = make(tmp_path, {"requirements.txt": "# deps\n--index-url https://x\n-r base.txt\nnumpy==1.26.4  # pinned\n"})
    assert D.dep_pinning(repo).raw_score == 80


def test_a6_javascript_lockfile_is_not_a_python_lockfile(tmp_path):
    assert D.dep_pinning(make(tmp_path, {"package-lock.json": "{}"})).raw_score == 0


def test_a6_binder_requirements(tmp_path):
    assert D.dep_pinning(make(tmp_path, {"binder/requirements.txt": "numpy==1.26.4\n"})).raw_score == 80


# A7 container specification ----------------------------------------------------------
def test_a7_def_file_needs_bootstrap(tmp_path):
    assert D.container_spec(make(tmp_path, {"src/exports.def": "LIBRARY mylib\nEXPORTS\n  f1\n"})).raw_score == 0
    assert D.container_spec(make(tmp_path / "b", {"image.def": "Bootstrap: docker\nFrom: python:3.11\n"})).raw_score == 80


@pytest.mark.parametrize("name,base", [("Dockerfile", "python@sha256:" + "a" * 64), ("Containerfile", "rocker/r-ver:4.3.1"),
                                       ("Dockerfile.gpu", "nvidia/cuda:12.2.0-runtime-ubuntu22.04"),
                                       ("Dockerfile", "jupyter/scipy-notebook:2023-10-20")])
def test_a7_pinned_bases_and_names(tmp_path, name, base):
    repo = make(tmp_path, {name: f"FROM {base}\nRUN pip install numpy\n"})
    assert D.container_spec(repo).raw_score == 100


def test_a7_latest_is_not_pinned(tmp_path):
    assert D.container_spec(make(tmp_path, {"Dockerfile": "FROM python:latest\nRUN pip install x\n"})).raw_score == 80


# A8 silent failure masking -----------------------------------------------------------
@pytest.mark.parametrize("code", ["try:\n    f()\nexcept: pass\n", "try:\n    f()\nexcept Exception as e: pass\n",
                                  "try:\n    f()\nexcept:\n    ...\n"])
def test_a8_except_pass_forms(tmp_path, code):
    assert D.silent_failure_masking(make(tmp_path, {"a.py": code})).raw_score == 0


def test_a8_specific_exception_is_fine(tmp_path):
    code = "try:\n    f()\nexcept KeyError:\n    pass\n"
    assert D.silent_failure_masking(make(tmp_path, {"a.py": code})).raw_score == 100


# B1 usage examples -----------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["jupyter nbconvert --to notebook --execute analysis.ipynb",
                                 "papermill in.ipynb out.ipynb", "jupyter lab", "python -m mypkg.run",
                                 "snakemake --cores 4"])
def test_b1_notebook_and_workflow_commands(tmp_path, cmd):
    repo = make(tmp_path, {"README.md": f"## Run\n```bash\n{cmd}\n```\n"})
    assert D.usage_examples(repo).raw_score == 100


def test_b1_command_outside_code_fence_does_not_count(tmp_path):
    repo = make(tmp_path, {"README.md": "```\nx = 1\n```\nLater you may run python main.py yourself.\n"})
    assert D.usage_examples(repo).raw_score == 60


def test_b1_prose_in_text_blocks_is_not_a_command(tmp_path):
    repo = make(tmp_path, {"README.md": "```txt\nDear Dr X,\nplease make the changes.\nMake sure to run it.\n```\n"})
    assert D.usage_examples(repo).raw_score == 60


def test_b1_make_target_and_prompt(tmp_path):
    assert D.usage_examples(make(tmp_path, {"README.md": "```\n$ make all\n```\n"})).raw_score == 100


def test_b1_large_readme_is_fast(tmp_path):
    big = "```python\nx = 1\n```\n" + "text line\n" * 20000 + "```\ncode\n```\n" * 2000
    repo = make(tmp_path, {"README.md": big})
    started = time.monotonic()
    D.usage_examples(repo)
    assert time.monotonic() - started < 0.3


# B2 docstring coverage -------------------------------------------------------------
def test_b2_methods_and_annotations(tmp_path):
    code = ('class A:\n    """Doc."""\n    def f(self) -> int:\n        """Doc."""\n        return 1\n'
            '    def g(self):\n        return 2\n    def _private(self):\n        return 3\n'
            "def h(x: int,\n      y: int) -> int:\n    '''Doc.'''\n    return x\n")
    ev = D.docstring_coverage(make(tmp_path, {"a.py": code}))
    assert ev.raw_score == 75.0          # A, f, h documented; g not; _private ignored


def test_b2_no_public_definitions_is_neutral(tmp_path):
    assert D.docstring_coverage(make(tmp_path, {"a.py": "x = 1\nprint(x)\n"})).raw_score == 50


# B3 headings ---------------------------------------------------------------------------
def test_b3_comments_in_code_blocks_are_not_sections(tmp_path):
    repo = make(tmp_path, {"README.md": "# Tool\n```bash\n# install dependencies\n# run the script\n```\n"})
    assert D.doc_structure(repo).raw_score == 0
    assert D.execution_entry_point(repo).raw_score == 0


def test_b3_prefix_words_are_not_sections(tmp_path):
    repo = make(tmp_path, {"README.md": "# Tool\n## Runtime notes\n## Database schema\n"})
    assert D.execution_entry_point(repo).raw_score == 0
    assert D.data_description(repo).raw_score == 0


@pytest.mark.parametrize("heading", ["## [∞](#quickstart) 5. Quickstart", "## 🚀 Installation", "### 2.1 Usage",
                                     "## Step 3: Run the analysis", "## **Requirements**"])
def test_b3_numbered_linked_and_emoji_headings(tmp_path, heading):
    repo = make(tmp_path, {"README.md": f"# Tool\n{heading}\ntext\n"})
    assert D.doc_structure(repo).raw_score == 25


def test_b3_rst_headings(tmp_path):
    repo = make(tmp_path, {"README.rst": "Tool\n====\n\nInstallation\n------------\n\npip install x\n\nUsage\n-----\n"})
    assert D.doc_structure(repo).raw_score == 50


# B5 data acquisition -----------------------------------------------------------------
@pytest.mark.parametrize("code", ['df = pd.read_csv("https://example.org/x.csv")',
                                  'urllib.request.urlretrieve("https://example.org/x.zip", "x.zip")',
                                  'ds = load_dataset("imdb")', 'mnist = MNIST("data", download=True)',
                                  'X = fetch_openml("mnist_784")', '!wget https://example.org/x.csv',
                                  'gdown.download(url, out)', 'p = tf.keras.utils.get_file("x", origin=url)'])
def test_b5_notebook_downloads(tmp_path, code):
    repo = make(tmp_path, {"nb.ipynb": nb(code)})
    assert D.data_acquisition_script(repo).raw_score == 100


def test_b5_load_data_in_docstring_is_not_a_download(tmp_path):
    code = 'def f():\n    """Loaded by the class DatasetObject.load_data()."""\n    return 1\n'
    assert D.data_acquisition_script(make(tmp_path, {"base.py": code})).raw_score == 0
    keras = "from tensorflow import keras\n(x, y), _ = keras.datasets.mnist.load_data()\n"
    assert D.data_acquisition_script(make(tmp_path / "k", {"k.py": keras})).raw_score == 100


def test_b5_installer_curl_is_not_data(tmp_path):
    repo = make(tmp_path, {"install.sh": "curl -sSL https://install.python-poetry.org | python3 -\n"})
    assert D.data_acquisition_script(repo).raw_score == 0


# B7 / B8 / B9 ---------------------------------------------------------------------------
def test_b7_word_click_is_not_a_cli(tmp_path):
    assert D.config_externalised(make(tmp_path, {"a.py": "# click the button\nx = 1\n"})).raw_score == 0
    assert D.config_externalised(make(tmp_path / "b", {"a.py": "import click\n@click.command()\ndef m(): pass\n"})).raw_score == 80


@pytest.mark.parametrize("name", ["license", "LICENSE-MIT", "LICENSE.MIT", "Licence.txt", "COPYING.LESSER"])
def test_b8_licence_names(tmp_path, name):
    assert D.reuse_metadata(make(tmp_path, {name: "MIT"})).raw_score == 40


def test_b8_zenodo_json_counts_as_metadata(tmp_path):
    repo = make(tmp_path, {"LICENSE": "x", "CITATION.cff": "x", ".zenodo.json": "{}"})
    assert D.reuse_metadata(repo).raw_score == 100


def test_b9_tensorflow_gpu_regex(tmp_path):
    repo = make(tmp_path, {"requirements.txt": "tensorflowXgpu\n"})
    assert D.hardware_requirements(repo).raw_score == 100      # not a GPU package


# C robustness ---------------------------------------------------------------------------
def test_c1_missing_explicit_rubric_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_rubric(tmp_path / "missing.yaml")


def test_c2_ros_with_partial_rubric(tmp_path):
    rubric = Rubric(name="partial", version="1", categories=load_rubric().categories, penalties=load_rubric().penalties,
                    ros_components={"I": {"weight": 0.5}, "N": {"weight": 0.5}}, rcs=load_rubric().rcs)
    result = ROSScorer(rubric).score(ExecutionEvidence(install_success=True, execution_success=True,
                                                       notebook_exec_rate=0.5, test_pass_rate=1.0))
    assert result.ros == 75.0 and set(result.available_components) == {"I", "N"}
