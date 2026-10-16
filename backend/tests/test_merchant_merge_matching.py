import copy
import unittest

from app.merchant_merge_matching import suggest_merges


def merchant(ident, name, *, aliases=(), kind='merchant', context='spending_card', **extra):
    return {'id': ident, 'canonical_name': name, 'kind': kind, 'category': 'Eating out',
            'spending_treatment': 'normal', 'n_transactions': 0, 'source': 'public_research',
            'aliases': [{'pattern': pattern, 'match_mode': 'exact', 'context': context} for pattern in aliases], **extra}


class MerchantMergeMatchingTests(unittest.TestCase):
    def test_case_punctuation_and_accents_discover_zero_payment_duplicate(self):
        result = suggest_merges([merchant(1, '% Áurelia', aliases=['Sq Aurelia Sample'], n_transactions=3),
                                 merchant(2, 'AURELIA', aliases=['SQ AURELIA SAMPLE'])])
        self.assertEqual(result['total'], 1)
        self.assertEqual((result['items'][0]['target_id'], result['items'][0]['source_id']), (1, 2))
        self.assertEqual(result['items'][0]['match_type'], 'same_name')

    def test_shared_exact_alias_is_a_clue_even_when_display_names_differ(self):
        result = suggest_merges([merchant(1, 'Corner Coffee', aliases=['SQ COFFEE LONDON']),
                                 merchant(2, 'The cafe by work', aliases=['Sq Coffee London'])])
        self.assertEqual(result['items'][0]['match_type'], 'shared_alias')

    def test_broad_person_alias_does_not_establish_a_duplicate(self):
        left = merchant(1, 'Alex Morgan', kind='person', aliases=['Alex'], context='transfer')
        right = merchant(2, 'Alex Jordan', kind='person', aliases=['Alex'], context='transfer')
        for item in (left, right):
            item['aliases'][0]['match_mode'] = 'prefix'
        self.assertEqual(suggest_merges([left, right])['items'], [])

    def test_missing_middle_name_is_explicitly_only_a_person_suggestion(self):
        result = suggest_merges([merchant(1, 'Casey Reed', kind='person', context='transfer', n_transactions=1),
                                 merchant(2, 'Casey Morgan Reed', kind='person', context='transfer', n_transactions=17)])
        self.assertEqual(result['items'][0]['match_type'], 'middle_name')
        self.assertIn('Confirm this is the same person', result['items'][0]['reason'])
        self.assertEqual(result['items'][0]['target_id'], 2)

    def test_shared_first_name_different_middle_names_and_joint_names_are_not_duplicates(self):
        for left, right in [('Casey Reed', 'Casey Taylor'), ('Casey Reed', 'Casey Rees'),
                            ('Casey Morgan Reed', 'Casey James Reed'), ('C Reed', 'Casey Reed'),
                            ('Casey Reed', 'Casey & Taylor Reed'), ('Casey Reed', 'Casey and Taylor Reed')]:
            with self.subTest(left=left, right=right):
                self.assertEqual(suggest_merges([merchant(1, left, kind='person'), merchant(2, right, kind='person')])['total'], 0)

    def test_business_processor_and_typo_clues_do_not_create_new_rules(self):
        items = [merchant(1, 'Bracken Kitchen'), merchant(2, 'SQ Bracken Kichen Ltd')]
        original = copy.deepcopy(items)
        result = suggest_merges(items)
        self.assertEqual(result['items'][0]['match_type'], 'similar_business_name')
        self.assertEqual(items, original)
        self.assertIn('Check that this is the same business', result['items'][0]['reason'])

    def test_generic_words_or_different_services_do_not_suggest_merging(self):
        for left, right in [('London Cafe', 'London Coffee'), ('Evermere', 'Evermere Housing'),
                            ('Amazon Music', 'Amazon Fresh'), ('Shop 12345', 'Shop 67890')]:
            with self.subTest(left=left, right=right):
                self.assertEqual(suggest_merges([merchant(1, left), merchant(2, right)])['total'], 0)

    def test_conflicting_treatments_kinds_or_card_transfer_contexts_are_excluded(self):
        base = merchant(1, 'Example', aliases=['Example'])
        incompatible = [merchant(2, 'Example', kind='person'),
                        merchant(3, 'Example', spending_treatment='self_transfer'),
                        merchant(4, 'Example', aliases=['Example'], context='spending_transfer'),
                        merchant(5, 'Example', aliases=['Example'], context='funding')]
        for other in incompatible:
            with self.subTest(other=other):
                self.assertEqual(suggest_merges([base, other])['total'], 0)

    def test_financial_duplicate_requires_account_reference_confirmation(self):
        items = [merchant(1, 'AMEX · card repayment', kind='financial', context='transfer',
                          spending_treatment='self_transfer', aliases=['AMERICAN EXPRESS FIRST PAYMENT']),
                 merchant(2, 'AMEX · card repayment', kind='financial', context='transfer',
                          spending_treatment='self_transfer', aliases=['AMERICAN EXP 0000 PB0000******00001'])]
        result = suggest_merges(items)
        self.assertEqual(result['total'], 1)
        self.assertIn('same provider name does not prove the same account', result['items'][0]['reason'])
        items[1]['canonical_name'] = 'AMEX · card 00001'
        self.assertEqual(suggest_merges(items)['total'], 0)

    def test_user_destination_then_transaction_alias_count_and_stable_id(self):
        items = [merchant(1, 'Example', n_transactions=100),
                 merchant(2, 'Example', source='user', aliases=['Example']),
                 merchant(3, 'Example', source='user', aliases=['Example', 'Extra'])]
        result = suggest_merges(items)['items']
        self.assertEqual(next(item['target_id'] for item in result if {item['source_id'], item['target_id']} == {1, 2}), 2)
        self.assertEqual(next(item['target_id'] for item in result if {item['source_id'], item['target_id']} == {2, 3}), 3)
        self.assertEqual(suggest_merges(items), suggest_merges(list(reversed(items))))
        self.assertEqual(suggest_merges([merchant(8, 'Example'), merchant(4, 'Example')])['items'][0]['target_id'], 4)

    def test_limit_counts_all_pairs_disabled_and_unnamed_records_are_ignored(self):
        items = [merchant(index, 'Example') for index in range(1, 5)]
        items += [merchant(5, 'Example', enabled=False), merchant(6, '!!!')]
        result = suggest_merges(items, limit=2)
        self.assertEqual((len(result['items']), result['total']), (2, 6))
        self.assertEqual(suggest_merges(items, limit=0), {'items': [], 'total': 6})

    def test_different_categories_are_flagged_for_destination_review(self):
        result = suggest_merges([merchant(1, 'Example'), merchant(2, 'Example', category='Shopping')])
        self.assertIn('categories differ', result['items'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
