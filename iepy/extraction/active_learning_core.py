import logging
import random
import sys

import numpy
from sklearn.cross_validation import StratifiedKFold
from sklearn.metrics import precision_recall_curve

from iepy import defaults
from iepy.extraction.relation_extraction_classifier import RelationExtractionClassifier


logger = logging.getLogger(__name__)


HIPREC = (10, 1)  # Precision is 10x more important than recall
HIREC = (1, 2)  # Recall is 2x more important than precision


class ActiveLearningCore:
    """
    IEPY's main class. Implements an active learning information extraction
    pipeline.

    From the user's point of view this class is meant to be used like this::

        extractor = ActiveLearningCore(relation, lbl_evidences)
        extractor.start()  # blocking
        while UserIsNotTired and extractor.questions:
            question = extractor.questions[0]
            answer = ask_user(question)
            extractor.add_answer(question, answer)
            extractor.process()
        predictions = extractor.predict()  # profit
    """

    #
    # IEPY User API
    #

    def __init__(self, relation, labeled_evidences, extractor_config=None,
                 performance_tradeoff=None, extractor=None, classifier=None):
        if extractor is None:
            extractor = RelationExtractionClassifier
        self.extractor = extractor
        self.relation = relation
        self.relation_classifier = classifier
        self._setup_labeled_evidences(labeled_evidences)
        self._questions = list(self.candidate_evidence)
        if extractor_config is None:
            extractor_config = defaults.extractor_config
        self.extractor_config = extractor_config
        self.tradeoff = performance_tradeoff
        self.aimed_tradeoff = None
        self.threshold = None

    def start(self):
        """
        Organizes the internal information, and prepares the first "questions" that
        need to be answered.
        """
        # API compliance. Nothing is done on current implementation.s
        pass

    @property
    def questions(self):
        """Returns a list of candidate evidences that would be good to have
        labels for.
        Order is important: labels for evidences listed firsts are more valuable.
        """
        return self._questions

    def add_answer(self, evidence, answer):
        """
        Not blocking.
        Informs to the Core the evidence label (True or False) decided
        from the outside.
        """
        assert answer in (True, False)
        self.labeled_evidence[evidence] = answer
        for list_ in (self._questions, self.candidate_evidence):  # TODO: Check performance. Should use set?
            list_.remove(evidence)
        # TODO: Save labeled evidence into database?

    def process(self):
        """
        Blocking.
        With all the labeled evidences, new questions are generated, optimizing the
        future gain of having those evidences labeled.
        After calling this method the values returned by `questions`
        and `predict` will change.
        """
        yesno = set(self.labeled_evidence.values())
        if len(yesno) > 2:
            msg = "Evidence is not binary! Can't proceed."
            logger.error(msg)
            raise ValueError(msg)
        if len(yesno) < 2:
            logger.debug("Not enough labels to train.")
            return
        if self.tradeoff:
            self.estimate_threshold()
        self.train_relation_classifier()
        self.rank_candidate_evidence()
        self.choose_questions()

    def predict(self):
        """
        Blocking (ie, not fast).
        With all the labeled evidence a classifier is trained and used for automatically
        labeling all other evidences.
        Returns a dict {evidence: True/False}, where the boolean label indicates if
        the relation is present on that evidence or not.
        """
        if not self.relation_classifier:
            logger.info("There is no classifier. Can't predict")
            return {}
        if self.threshold is None:
            labels = self.relation_classifier.predict(self.candidate_evidence)
        else:
            scores = self.relation_classifier.decision_function(self.candidate_evidence)
            labels = scores >= self.threshold
        prediction = dict(zip(self.candidate_evidence, map(bool, labels)))
        prediction.update(self.labeled_evidence)
        return prediction

    def estimate_threshold(self):
        scores, y_true = self.get_kfold_data()
        if scores is None:
            return
        prec, rec, thres = precision_recall_curve(y_true, scores)
        prec[-1] = 0.0  # To avoid choosing the last phony value
        c_prec, c_rec = self.tradeoff
        # Below is a linear function on precision and recall, expressed using
        # numpy notation, we're maximizing it.
        i = (prec * c_prec + rec * c_rec).argmax()  # Index of the maximum
        assert i < len(thres)  # Because prec[-1] is 0.0
        self.aimed_tradeoff = (prec[i], rec[i])
        self.threshold = thres[i]
        s = "Using {} samples, threshold aiming at precision={:.4f} and recall={:.4f}"
        logger.debug(s.format(len(scores), prec[i], rec[i]))

    # Instance attributes:
    # questions: A list of evidence
    # ranked_candidate_evidence: A dict candidate_evidence -> float
    # aimed_tradeoff: A (prec, rec) tuple with the precision/recall tradeoff
    #                 that the threshold aims to achieve.

    #
    # Private methods
    #

    def _setup_labeled_evidences(self, labeled_evidences):
        self.candidate_evidence = []
        self.labeled_evidence = {}
        for e, lbl in labeled_evidences.items():
            if lbl is None:
                self.candidate_evidence.append(e)
            else:
                self.labeled_evidence[e] = bool(lbl)
        if not self.candidate_evidence:
            raise ValueError("Cannot start core without candidate evidence")
        logger.info("Loaded {} candidate evidence and {} labeled evidence".format(
                    len(self.candidate_evidence), len(self.labeled_evidence)))

    def train_relation_classifier(self):
        X = []
        y = []
        for evidence, score in self.labeled_evidence.items():
            X.append(evidence)
            y.append(int(score))
            assert y[-1] in (True, False)
        self.relation_classifier = self.extractor(**self.extractor_config)
        self.relation_classifier.fit(X, y)

    def rank_candidate_evidence(self):
        if not self.candidate_evidence:
            self.ranked_candidate_evidence = {}
            logger.info("No evidence left to rank.")
            return
        N = min(10 * len(self.labeled_evidence), len(self.candidate_evidence))
        logger.info("Ranking a sample of {} candidate evidence".format(N))
        sample = random.sample(self.candidate_evidence, N)
        ranks = self.relation_classifier.decision_function(sample)
        self.ranked_candidate_evidence = dict(zip(self.candidate_evidence, ranks))
        ranks = [abs(x) for x in ranks]
        logger.info("Ranking completed, lowest absolute rank={}, "
                    "highest absolute rank={}".format(min(ranks), max(ranks)))

    def choose_questions(self):
        # Criteria: Answer first candidates with decision function near 0
        # because they are the most uncertain for the classifier.
        self._questions = sorted(self.ranked_candidate_evidence,
                                 key=lambda x: abs(self.ranked_candidate_evidence[x]))

    def get_kfold_data(self):
        """
        Perform k-fold cross validation and return (scores, y_true) where
        scores is a numpy array with decision function scores and y_true
        is a numpy array with the true label for that evidence.
        """
        allX = []
        ally = []
        for evidence, score in self.labeled_evidence.items():
            allX.append(evidence)
            ally.append(int(score))
            assert ally[-1] in (True, False)
        allX = numpy.array(allX)
        ally = numpy.array(ally)
        if numpy.bincount(ally).min() < 5:
            return None, None  # Too little data to do 5-fold cross validation

        logger.debug("Performing 5-fold cross validation")
        scores = []
        y_true = []
        for train_index, test_index in StratifiedKFold(ally, 5):
            X = allX[train_index]
            y = ally[train_index]
            c = self.extractor(**self.extractor_config)
            c.fit(X, y)
            y_true.append(ally[test_index])
            scores.append(c.decision_function(allX[test_index]))
        return numpy.hstack(scores), numpy.hstack(y_true)
