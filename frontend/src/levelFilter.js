export const LEVEL_OPTIONS = ['无', '培优', '奥数', '竞赛', '全部', '不限']
const LEVEL_COLUMNS = { 培优: 'N', 奥数: 'O', 竞赛: 'P' }

export function toggleLevel(selected = [], option) {
  if (option === '不限') return []
  if (option === '无' || option === '全部') return [option]
  const levels = selected.filter((level) => level in LEVEL_COLUMNS)
  return levels.includes(option) ? levels.filter((level) => level !== option) : [...levels, option]
}

export function matchesLevels(values, selected = []) {
  const marked = (level) => values[LEVEL_COLUMNS[level]]?.trim() === '要'
  if (!selected.length) return true
  if (selected.includes('无')) return !Object.keys(LEVEL_COLUMNS).some(marked)
  if (selected.includes('全部')) return Object.keys(LEVEL_COLUMNS).every(marked)
  return selected.some(marked)
}