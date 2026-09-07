import { test } from 'node:test'
import assert from 'node:assert/strict'
import { matchesLevels, toggleLevel } from './levelFilter.js'

test('exclusive options replace selections and unrestricted is the default', () => {
  assert.deepEqual(toggleLevel(['培优', '奥数'], '无'), ['无'])
  assert.deepEqual(toggleLevel(['无'], '全部'), ['全部'])
  assert.deepEqual(toggleLevel(['全部'], '不限'), [])
  assert.deepEqual(toggleLevel(['无'], '培优'), ['培优'])
  assert.deepEqual(toggleLevel(['全部'], '竞赛'), ['竞赛'])
  assert.deepEqual(toggleLevel(undefined, '奥数'), ['奥数'])
})

test('individual levels can be combined and deselected', () => {
  assert.deepEqual(toggleLevel(['培优'], '奥数'), ['培优', '奥数'])
  assert.deepEqual(toggleLevel(['培优', '奥数'], '培优'), ['奥数'])
  assert.deepEqual(toggleLevel(['奥数'], '奥数'), [])
})

test('all eight level combinations match none, all and union selections', () => {
  for (let mask = 0; mask < 8; mask += 1) {
    const values = { N: mask & 1 ? '要' : '', O: mask & 2 ? '要' : '', P: mask & 4 ? '要' : '' }
    assert.equal(matchesLevels(values), true)
    assert.equal(matchesLevels(values, ['无']), mask === 0)
    assert.equal(matchesLevels(values, ['全部']), mask === 7)
    assert.equal(matchesLevels(values, ['培优', '奥数']), Boolean(mask & 3))
    assert.equal(matchesLevels(values, ['竞赛']), Boolean(mask & 4))
  }
  assert.equal(matchesLevels({ N: '否' }, ['无']), true)
})