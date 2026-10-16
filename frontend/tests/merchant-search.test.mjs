import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { searchMerchants, searchMerchantResult } = await sourceModule("merchantSearch");
const merchant = (id, canonical_name, aliases = []) => ({ id, canonical_name, aliases: aliases.map(pattern => ({ pattern })) });
const ids = items => items.map(item => item.id);

test("fuzzy search ranks exact saved names then exact aliases before partial or typo matches", () => {
  const items = [merchant(1, "Bracken Kichen"), merchant(2, "SQ Bracken Kitchen London"),
    merchant(3, "Cookware shop", ["Bracken Kitchen"]), merchant(4, "Bracken Kitchen")];
  assert.deepEqual(ids(searchMerchants(items, "Bracken Kitchen")), [4, 3, 2, 1]);
});

test("search ignores case, accents, punctuation, and spacing", () => {
  assert.deepEqual(ids(searchMerchants([merchant(1, "% Áurelia")], "  aurelia  ")), [1]);
  assert.deepEqual(ids(searchMerchants([merchant(1, "Caffè-Nero")], "CAFFE NERO")), [1]);
  assert.deepEqual(ids(searchMerchants([merchant(1, "AMEX")], "A.M.E.X")), [1]);
});

test("first and last name search finds the version with an extra middle name", () => {
  const items = [merchant(1, "Casey Morgan Reed"), merchant(2, "Casey Taylor"), merchant(3, "Casey Reed")];
  assert.deepEqual(ids(searchMerchants(items, "Casey Reed")), [3, 1]);
});

test("typos and transpositions are useful search clues but short names and numbers stay exact", () => {
  assert.deepEqual(ids(searchMerchants([merchant(1, "Bracken Kitchen")], "Brakcen Kitchen")), [1]);
  assert.deepEqual(searchMerchants([merchant(1, "Amy")], "May"), []);
  assert.deepEqual(searchMerchants([merchant(1, "Card 81043")], "Card 81044"), []);
});

test("long-name typo matching keeps its two-edit boundary and stable rankings", () => {
  const items = [merchant(1, "bacdefhg"), merchant(2, "abcxdefgh"), merchant(3, "abcdefgh"),
    merchant(4, "abxycdefgh"), merchant(5, "badcfegh"), merchant(6, "abefgh")];
  assert.deepEqual(ids(searchMerchants(items, "abcdefgh")), [3, 2, 1, 4]);
  assert.deepEqual(ids(searchMerchants([merchant(1, "abefghij")], "abcdefghij")), [1]);
});

test("long pasted terms can match labels and aliases without a quadratic search matrix", () => {
  const prefix = "a".repeat(20000);
  const items = [merchant(1, `${prefix}bc`), merchant(2, "Saved label", [`${prefix}cb`]), merchant(3, `${prefix}xyzq`)];
  assert.deepEqual(ids(searchMerchants(items, `${prefix}cb`)), [2, 1]);
});

test("every requested token must occur within one saved label", () => {
  const items = [merchant(1, "Casey", ["Reed"]), merchant(2, "Casey Reed"), merchant(3, "Reed", ["Casey"] )];
  assert.deepEqual(ids(searchMerchants(items, "Casey Reed")), [2]);
});

test("empty searches preserve original order and objects without mutating input", () => {
  const first = merchant(9, "B"), second = merchant(1, "A");
  const items = Object.freeze([Object.freeze(first), Object.freeze(second)]);
  const result = searchMerchants(items, "   ");
  assert.deepEqual(result, items);
  assert.equal(result[0], first);
  assert.notEqual(result, items);
});

test("text search permits normalized substrings and does not broaden to typo matches", () => {
  const items = [merchant(1, "Bracken Kitchen"), merchant(2, "Bracken Kichen")];
  assert.deepEqual(ids(searchMerchantResult(items, "kitchen", "text").items), [1]);
  assert.deepEqual(ids(searchMerchantResult([merchant(1, "Caffè Nero")], "CAFFE", "text").items), [1]);
});

test("regex mode supports anchored groups, alternatives, and case-insensitive aliases", () => {
  const items = [merchant(1, "Casey Morgan Reed"), merchant(2, "Casey Reed"), merchant(3, "Reed"),
    merchant(4, "Card repayment", ["AMERICAN EXP 0000 PB0000******00001"])];
  assert.deepEqual(ids(searchMerchantResult(items, "^casey( .+)? reed$", "regex").items), [1, 2]);
  assert.deepEqual(ids(searchMerchantResult(items, "amex|american exp", "regex").items), [4]);
  assert.deepEqual(ids(searchMerchantResult(items, "[0-9]{4} PB", "regex").items), [4]);
});

test("regex searches each original name and alias separately", () => {
  const items = [merchant(1, "AMEX", ["First Payment"]), merchant(2, "% Áurelia")];
  assert.deepEqual(searchMerchantResult(items, "AMEX.*First", "regex").items, []);
  assert.deepEqual(ids(searchMerchantResult(items, "^% Áurelia$", "regex").items), [2]);
});

test("invalid or unsupported regex reports inline error and no results", () => {
  for (const expression of ["[", "(?<=amex)card", "(a)\\1"]) {
    const result = searchMerchantResult([merchant(1, "anything")], expression, "regex");
    assert.deepEqual(result.items, []);
    assert.match(result.error, /Invalid or unsupported/);
  }
  assert.match(searchMerchantResult([], "a".repeat(129), "regex").error, /128/);
});

test("backtracking-shaped regex is handled safely by the linear-time matcher", () => {
  const longLabel = "a".repeat(20000) + "!";
  const result = searchMerchantResult([merchant(1, longLabel)], "(a+)+$", "regex");
  assert.deepEqual(result, { items: [], error: null });
});

test("switching search modes reuses the same query without saving any matching rule", () => {
  const items = [merchant(1, "AMEX"), merchant(2, "American Express")];
  const original = structuredClone(items);
  assert.deepEqual(searchMerchantResult(items, "amex|american", "text").items, []);
  assert.deepEqual(ids(searchMerchantResult(items, "amex|american", "regex").items), [1, 2]);
  assert.deepEqual(items, original);
});
