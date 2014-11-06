"""
Run IEPY rule-based extractor

Usage:
    iepy_rules_runner.py
    iepy_rules_runner.py -h | --help | --version

Picks from rules.py the relation to work with, and the rules definitions and
proceeds with the extraction.

Options:
  -h --help             Show this screen
  --version             Version number
"""
import sys
import logging

from django.core.exceptions import ObjectDoesNotExist

import iepy
iepy.setup(__file__)

from iepy.extraction.rules_core import RuleBasedCore
from iepy.data import models, output
from iepy.data.db import CandidateEvidenceManager


def run_from_command_line():
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    try:
        relation_name = iepy.instance.rules.RELATION
    except AttributeError:
        logging.error("RELATION not defined in rules file")
        sys.exit(1)

    try:
        relation = models.Relation.objects.get(name=relation_name)
    except ObjectDoesNotExist:
        logging.error("Relation {!r} not found".format(relation_name))
        sys.exit(1)

    # Load rules
    rules = []
    for attr_name in dir(iepy.instance.rules):
        attr = getattr(iepy.instance.rules, attr_name)
        if hasattr(attr, '__call__'):  # is callable
            if hasattr(attr, "is_rule") and attr.is_rule:
                rules.append(attr)

    # Load evidences
    evidences = CandidateEvidenceManager.candidates_for_relation(relation)

    # Run the pipeline
    iextractor = RuleBasedCore(relation, evidences, rules)
    iextractor.start()
    iextractor.process()
    predictions = iextractor.predict()
    output.dump_output_loop(predictions)


if __name__ == u'__main__':
    run_from_command_line()
