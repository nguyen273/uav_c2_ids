"""Deprecated unsafe Maeng fitter.

The old implementation treated uncalibrated IQ level as RSRP and computed
in-sample error. It is intentionally disabled so stale output cannot be
mistaken for a propagation calibration. Use P3_layer1_propagation_gurses.py,
whose output is explicitly marked exploratory and relative-only.
"""
import argparse
import logging
import sys

logging.basicConfig(level=logging.ERROR)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.error("This legacy fitter is disabled: it produced uncalibrated, mislabeled RSRP and in-sample metrics.")
    sys.exit(2)
