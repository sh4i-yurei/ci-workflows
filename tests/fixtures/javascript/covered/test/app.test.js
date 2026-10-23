import test from 'node:test';
import assert from 'node:assert/strict';
import { classify, witness } from '../src/app.js';

test('both branches are checked', () => {
  assert.equal(classify(2), 'positive');
  assert.equal(classify(-2), 'not positive');
  assert.equal(classify(0), 'not positive');
  assert.equal(witness(), 'covered');
});
