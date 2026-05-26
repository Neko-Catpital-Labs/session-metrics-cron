# Setup

## Prerequisites

- Python 3.9+
- `npx` (for `ccusage`)
- `ssh` + `rsync` for remote sources
- Mixpanel project credentials

Optional:

- `python3 -m pip install pyyaml` (required only if you choose YAML source config)

## Configure

1. Create runtime env file:

```bash
cp config/nightly-usage.env.example config/nightly-usage.env
```

2. Create host/source inventory:

```bash
cp config/sources.json config/sources.local.json
```

3. Point env to the local inventory file:

```bash
USAGE_PIPELINE_SOURCES_CONFIG=$PWD/config/sources.local.json
```

4. Run dry run:

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env
```
