# Automated LLM-Based Architectural Refactoring Pipeline

This repository contains the experimental pipeline used to analyze and refactor architectural dependency cycles in real-world open-source software projects using LLMs.

The pipeline automatically extracts dependency graphs, identifies strongly connected components, selects architectural cycles, produces an explanation of the cycles using a multi-agent LLM approach, applies LLM-based refactoring using OpenHands, and evaluates the results using architectural and code-quality metrics. All experiments are executed inside Docker and are designed to run fully offline with respect to version control.

The repository is intended as a replication package for a master’s thesis and related research work, and for anyone who wish to run similar experiments.

---

## Platform

The pipeline was developed and executed on a Windows host system using an Ubuntu environment (WSL) and Docker. All experiments were run inside Linux containers, with the project repository stored inside the Linux filesystem.

Compatibility with other platforms has not been tested.

---

## Overview of the Pipeline

Each experiment consists of four main phases.

First, the baseline phase extracts module-level dependency graphs and computes strongly connected components and baseline metrics for each project.

Second, a cycle selection phase builds a catalog of architectural dependency cycles and selects a subset for analysis.

Third, a multi-agent LLM team tries to explain the cycle.

Fourth, the refactoring phase uses OpenHands and a configured LLM endpoint to attempt automated refactoring using the explanation generated in the last step. For each selected cycle, a local Git branch is created, OpenHands is executed, and all file changes are committed locally.

Finally, a metrics phase recomputes architectural and code-quality metrics on the refactored code.

All configuration is controlled through a YAML file.

---

## Configuration

Experiments are configured using a YAML file located in `configs/`, for example `configs/pipeline.yaml`.

This file specifies the locations of the analyzed repositories, the repository list, the cycle list, the results directory, the experiment identifier, and the parameters used for OpenHands and the LLM.

An example configuration is shown below:

```yaml
projects_dir: projects_to_analyze
repos_file: repos.txt
cycles_file: cycles_to_analyze.txt
results_root: results
experiment_id: expA

llm:
  base_url: "http://host.docker.internal:8012/v1"
  api_key: "placeholder"
  model_raw: "/path/to/model"

openhands:
  image: "docker.all-hands.dev/all-hands-ai/openhands:0.59"
  runtime_image: "docker.all-hands.dev/all-hands-ai/runtime:0.59-nikolaik"
  max_iters: 100
  commit_message: "Refactor: break dependency cycle"

modes:
  - id: no_explain
    params:
      orchestrator: minimal

  - id: explain_E0_S0_noaux
    params:
      orchestrator: multi_agent
      edge_variant: E0
      synthesizer_variant: S0
      auxiliary_agent: none

  - id: explain_E1_S0_boundary
    params:
      orchestrator: multi_agent
      edge_variant: E1
      synthesizer_variant: S0
      auxiliary_agent: boundary
```

Users must provide their own OpenAI-compatible LLM endpoint. The endpoint must be reachable from inside the Docker container and support the `/v1/chat/completions` API.

No GitHub credentials are required, as all experiments operate on local branches only.

---

## Repository Preparation

All subject systems must be cloned manually into the directory specified by `projects_dir` (by default `projects_to_analyze/`). The pipeline does not automatically download repositories.

Each repository must correspond to an entry in `repos.txt`, which specifies the repository name, base branch, entry directory, and implementation language.

Supported languages are `python`, `csharp`, and `javascript`.

The cycle list used for refactoring is stored in `cycles_to_analyze.txt`. This file is generated automatically using the provided scripts and should not normally be edited manually, unless you want specific cycles to be excluded/included.

---

## JavaScript and TypeScript Support

The pipeline supports JavaScript and TypeScript projects using `javascript` as the language identifier in `repos.txt`. Both `.js` and `.ts` files are handled transparently.

### Dependency Graph Extraction

Dependency graphs are extracted using [dependency-cruiser](https://github.com/sverweij/dependency-cruiser), which resolves `import`/`require` statements to actual files. The extraction script (`ATD_identification/analyze_cycles_jsts.sh`) handles:

* **tsconfig path aliases** — reads `compilerOptions.paths` (e.g. `$lib/*`, `$apis/*`) from `tsconfig.json` and resolves aliased imports to real files on disk. This covers SvelteKit, Next.js, and any project using TypeScript path aliases.
* **SvelteKit auto-detection** — if `svelte.config.js` is present and `.svelte-kit/` has not been generated, the script runs `npx svelte-kit sync` automatically to produce the generated `tsconfig.json` with framework path aliases.
* **Monorepo detection** — automatically detects npm/Yarn workspaces (`package.json` workspaces field), pnpm workspaces (`pnpm-workspace.yaml`), and Lerna (`lerna.json`). When detected, `dependency-cruiser` is run across all workspace packages with `--include-only` scoping.
* **Optional per-repo config** — custom `.dependency-cruiser.js` config files can be placed in `ATD_identification/depcruise-configs/` and passed via `--depcruise-config`. When no config is provided, sensible defaults are used.

### Quality Collection

Quality checks are minimal for v1: `npm test` (auto-detecting Jest, Vitest, or Mocha) and `eslint --format json`. Per-repo test setup scripts can be placed in `code_quality_checker/repo-test-setups-jsts/`.

### Known Limitations

* **Monorepos with per-package tsconfig path aliases** — when workspace packages use different `compilerOptions.paths`, some cross-package imports may not resolve correctly. This is a known limitation of `dependency-cruiser` and is tracked for future work.
* **Virtual modules** — SvelteKit virtual modules (`$app/*`, `$env/*`) are correctly excluded from the dependency graph.

### Test Case

The included test repo `SINDIT20-Frontend` (SvelteKit + TypeScript) validates the extraction pipeline. Run the assertions with:

```bash
python3 test_runs/assert_toyjsts_edges.py
```

See `docs/future_jsts_enhancements.md` for planned improvements including `source_parser` supplementation, expanded quality metrics, and full monorepo support.

---

## Building and Running the Container

The Docker image is built using the provided `Dockerfile`:

```bash
docker build --target dev -t atd-dev .
```

All experiments are executed inside this container.

The container is started using:

```bash
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)

docker run --rm -it \
  --add-host=host.docker.internal:host-gateway \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd)":/workspace \
  -w /workspace \
  --name atd-dev \
  --user "$(id -u):$(id -g)" \
  --group-add "$DOCKER_GID" \
  -e HOST_PWD="$(pwd)" \
  atd-dev
```

This configuration allows OpenHands to launch nested containers and ensures correct file ownership.

---

## Running Experiments

Baseline analysis is performed using:

```bash
scripts/run_baseline.sh -c configs/pipeline.yaml
```

Cycle selection is performed using:

```bash
scripts/build_cycles_to_analyze.sh -c configs/pipeline.yaml \
  --total 100 \
  --min-size 2 \
  --max-size 8 \
  --out cycles_to_analyze.txt
```

The `--strategy` flag controls how cycles are selected:

* `balanced` (default) — distributes evenly across cycle sizes with fair repo representation. Designed for experimental breadth.
* `importance` — ranks all candidate cycles by average PageRank and selects the top N. Cycles involving the most central, highly-depended-on modules are picked first.

Example using importance-based selection:

```bash
scripts/build_cycles_to_analyze.sh -c configs/pipeline.yaml \
  --total 10 \
  --min-size 2 \
  --max-size 5 \
  --strategy importance \
  --out cycles_to_analyze.txt
```

LLM-based refactoring is performed using:

```bash
scripts/run_llm.sh -c configs/pipeline.yaml --modes explain_multiAgent1 --modes explain_multiAgent2

scripts/run_llm.sh -c configs/pipeline.yaml --modes explain_E0_S0_noaux
```

Post-refactoring metrics are collected using:

```bash
scripts/run_metrics.sh -c configs/pipeline.yaml --modes explain_multiAgent1 --modes explain_multiAgent2
```

---

## Local Branch and Commit Policy

Each OpenHands run operates on a temporary Git worktree and a dedicated local branch. All changes are committed locally and never pushed to remote repositories.

No GitHub authentication is required, and running large-scale experiments does not affect remote repositories.

For each run, the complete patch produced by the LLM is stored in the results directory. After completion, the temporary worktree is removed to prevent accidental reuse.

---

## Results Structure

All experimental outputs are stored under the directory specified by `results_root` in the configuration file.

For each repository, branch, and experimental mode, the results include:

* Explanation prompt + usage + transcript
* OpenHands execution logs and trajectories
* Git patch files containing all code changes
* Dependency graphs and SCC reports
* Test results and code-quality metrics
* Status and metadata files

These artifacts make it possible to inspect, reproduce, and audit each individual refactoring attempt.

---

## Smoke Testing and Resume Validation

The repository includes an automated smoke test suite for validating fault tolerance and resume behavior under realistic LLM failure conditions.

These tests use a controlled fake OpenAI-compatible server to simulate LLM unavailability and verify that:

* Incomplete phases are detected
* Partial results are discarded safely
* Completed work is not recomputed
* Interrupted experiments can be resumed correctly

Detailed documentation and test scenarios are provided in:

```
test_runs/README.md
```


---

## Reproducibility

All experiments are executed inside Docker with pinned tool versions. The pipeline stores complete logs, trajectories, diffs, and metrics for each run.

No remote Git operations are performed, and all experimental artifacts are preserved locally. This design enables full reproducibility and post-hoc inspection of all refactoring attempts.

