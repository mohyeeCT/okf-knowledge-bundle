"""Exact outgoing-text checks, independent of content approval/state."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from okf_bundle.cli import main
from okf_bundle.summary import check_summary


class ReviewSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'review.md'

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')

    def test_exact_limit_and_over_limit(self):
        for count in (150, 151, 159):
            with self.subTest(count=count):
                self.write(' '.join(['statement'] * count))
                result = check_summary(self.path)
                self.assertEqual(result['word_count'], count)
                self.assertEqual(result['valid'], count <= 150)

    def test_whole_response_including_preface_and_closing(self):
        self.write('I completed the review.\n\n' + ' '.join(['statement'] * 144) + '\n\nEverything stays unpublished.')
        self.assertFalse(check_summary(self.path)['valid'])

    def test_final_edit_invalidates_original_hash_even_when_still_short(self):
        self.write('Reviewed 17 pages. Read the [wording preview](https://example.com/preview). Everything stays unpublished.')
        before = check_summary(self.path)
        self.write(before['text'].replace('17', '18'))
        self.assertFalse(check_summary(self.path, before['sha256'])['valid'])
        self.assertTrue(check_summary(self.path)['valid'])

    def test_markdown_labels_headings_and_entity_spaces(self):
        self.write('# Review\n\n1. **Office address:** see [two source pages](https://example.com/source).\n\nKeep&nbsp;everything unpublished.')
        self.assertEqual(check_summary(self.path)['word_count'], 11)

    def test_empty_and_hidden_html_not_accepted(self):
        self.write('  \n')
        self.assertFalse(check_summary(self.path)['valid'])
        self.write('<p>one</p><p>two</p>')
        with self.assertRaises(ValueError):
            check_summary(self.path)

    def test_cli_without_run_returns_exact_text_and_failure_exit(self):
        for count in (10, 151):
            self.write(' '.join(['word'] * count))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['review-summary', '--file', str(self.path)])
            result = json.loads(output.getvalue())
            self.assertEqual(result['text'], self.path.read_text(encoding='utf-8'))
            self.assertEqual(code, 0 if count <= 150 else 2)

    def test_sentence_ending_zip_code_counts_as_content(self):
        self.write('Our postal code is 77402.')
        self.assertEqual(check_summary(self.path)['word_count'], 5)

    def test_numeric_tokens_count_in_every_position(self):
        for text, count in [('77402.', 1), ('The limit is 17.', 4),
                            ('The fee is 150.', 4), ('2026) remains a content token.', 5)]:
            with self.subTest(text=text):
                self.write(text)
                result = check_summary(self.path)
                self.assertEqual(result['word_count'], count)
                self.assertTrue(result['valid'])

    def test_ordered_list_markers_are_counted_conservatively(self):
        self.write('1. One item\n2) Another item')
        self.assertEqual(check_summary(self.path)['word_count'], 6)

    def test_final_newline_does_not_change_count_or_strip_number(self):
        results = []
        for ending in ('', '\n'):
            text = 'Our postal code is 77402.' + ending
            self.path.write_bytes(text.encode('utf-8'))
            result = check_summary(self.path)
            self.assertEqual(result['word_count'], 5)
            self.assertEqual(result['text'], text)
            results.append(result)
        self.assertNotEqual(results[0]['sha256'], results[1]['sha256'])

    def test_numeric_last_word_at_exact_limit_is_accepted(self):
        self.write(' '.join(['word'] * 149 + ['77402.']))
        result = check_summary(self.path)
        self.assertEqual(result['word_count'], 150)
        self.assertTrue(result['valid'])

    def test_numeric_last_word_cannot_hide_over_limit_response(self):
        self.write(' '.join(['word'] * 150 + ['77402.']))
        result = check_summary(self.path)
        self.assertEqual(result['word_count'], 151)
        self.assertFalse(result['valid'])

    def test_multiple_sentence_ending_numbers_cannot_hide_overflow(self):
        self.write(' '.join(['word'] * 148 + ['17.', '150.', '77402.']))
        result = check_summary(self.path)
        self.assertEqual(result['word_count'], 151)
        self.assertFalse(result['valid'])

    def test_cli_rejects_numeric_overflow_and_preserves_exact_text(self):
        text = ' '.join(['word'] * 150 + ['77402.'])
        self.write(text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['review-summary', '--file', str(self.path)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(result['valid'])
        self.assertEqual(result['word_count'], 151)
        self.assertEqual(result['text'], text)
