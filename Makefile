PYTHON ?= python3
ENV_FILE ?= config/nightly-usage.env
SOURCES_CONFIG ?= config/sources.json
REPORT_DATE ?=

.PHONY: audit report export-dry-run nightly-dry-run test lint

audit:
	$(PYTHON) scripts/cache_hit_audit.py --output cache-hit-audit-report.json --top 50 --sources-config "$(SOURCES_CONFIG)"

report:
	$(PYTHON) scripts/planning_vs_execution_report.py --out-dir reports

export-dry-run:
	MIXPANEL_TOKEN=$${MIXPANEL_TOKEN:-dryrun-token} $(PYTHON) scripts/mixpanel_export_usage.py --dry-run --input-root . $(if $(REPORT_DATE),--date $(REPORT_DATE),)

nightly-dry-run:
	bash scripts/nightly_usage_pipeline.sh --dry-run --env-file "$(ENV_FILE)"

test:
	bash scripts/test-nightly-usage-pipeline.sh

lint:
	$(PYTHON) -m py_compile scripts/cache_hit_audit.py scripts/planning_vs_execution_report.py scripts/mixpanel_export_usage.py
	bash -n scripts/nightly_usage_pipeline.sh scripts/install-nightly-usage-launchd.sh scripts/uninstall-nightly-usage-launchd.sh scripts/test-nightly-usage-pipeline.sh
