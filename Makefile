.PHONY: check test

check:
	python3 -S -B tools/upstreams.py check
	python3 -S -B plugins/delivery-harness/scripts/validate_bundle.py plugins/delivery-harness
	git diff --check

test:
	python3 -S -B -m unittest discover -s tools/tests -q
	cd plugins/delivery-harness && python3 -S -B -m unittest discover -s tests -t . -q
