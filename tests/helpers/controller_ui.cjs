'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function readStatic(name = 'app.js', root = path.join(__dirname, '../..')) {
  return fs.readFileSync(path.join(root, 'server/static', name), 'utf8');
}

// Bind immutable source text per consumer; VM state and mocks remain local.
// Preserve async declarations and the existing next-declaration boundary.
function functionLoader(source) {
  return name => {
    const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
    assert.ok(match, `actual UI function ${name} exists`);
    const next = source.slice(match.index + 1).search(/\n(?:async )?function /);
    return source.slice(match.index, next < 0 ? undefined : match.index + next + 1);
  };
}

function constantLoader(source) {
  return name => {
    const match = source.match(new RegExp(`^const ${name} = [^\\n]+;`, 'm'));
    assert.ok(match, `actual UI constant ${name} exists`);
    return match[0];
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function nextTurn() {
  return new Promise(resolve => setImmediate(resolve));
}

module.exports = {readStatic, functionLoader, constantLoader, deferred, nextTurn};
