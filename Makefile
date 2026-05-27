PYTHON ?= python3
ENV_FILE ?= config/nightly-usage.env
SOURCES_CONFIG ?= config/sources.json
REPORT_DATE ?=

.PHONY: audit report export-dry-run nightly-dry-run test benchmark-dry-run lint

audit:
	$(PYTHON) scripts/cache_hit_audit.py --output cache-hit-audit-report.json --top 50 --sources-config "$(SOURCES_CONFIG)"

report:
	$(PYTHON) scripts/planning_vs_execution_report.py --out-dir reports

export-dry-run:
	MIXPANEL_TOKEN=$${MIXPANEL_TOKEN:-dryrun-token} $(PYTHON) scripts/mixpanel_export_usage.py --dry-run --input-root . $(if $(REPORT_DATE),--date $(REPORT_DATE),)

nightly-dry-run:
	bash scripts/nightly_usage_pipeline.sh --dry-run --env-file "$(ENV_FILE)"

test:
	$(PYTHON) scripts/test-usage-costing.py
	$(PYTHON) scripts/test-task-categorization.py
	$(PYTHON) scripts/test-request-pattern-categorization.py
	bash scripts/test-nightly-usage-pipeline.sh
	bash scripts/test-invoker-benchmark-dry-run.sh

benchmark-dry-run:
	bash scripts/test-invoker-benchmark-dry-run.sh

lint:
	$(PYTHON) -m py_compile scripts/cache_hit_audit.py scripts/planning_vs_execution_report.py scripts/mixpanel_export_usage.py scripts/usage_costing.py scripts/test-usage-costing.py scripts/test-task-categorization.py scripts/test-request-pattern-categorization.py scripts/request_pattern_quality_report.py scripts/mixpanel_dashboard_migration.py
	bash -n scripts/nightly_usage_pipeline.sh scripts/install-nightly-usage-launchd.sh scripts/uninstall-nightly-usage-launchd.sh scripts/test-nightly-usage-pipeline.sh scripts/test-invoker-benchmark-dry-run.sh invoker-benchmarks/bin/run-nightly-benchmark.sh invoker-benchmarks/bin/run-worker-job.sh invoker-benchmarks/bin/emit-mixpanel-events.sh invoker-benchmarks/bin/sync-worker-credentials.sh
