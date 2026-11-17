// 校验前端页面从 api/index.js 导入的函数是否都已导出
import fs from 'fs'
import path from 'path'

const apiSrc = fs.readFileSync('src/api/index.js', 'utf8')
const exported = new Set()
for (const m of apiSrc.matchAll(/export\s+const\s+([A-Za-z0-9_$]+)/g)) exported.add(m[1])
for (const m of apiSrc.matchAll(/export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)/g)) exported.add(m[1])

const used = new Map()
function walk(dir) {
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f)
    const st = fs.statSync(p)
    if (st.isDirectory()) walk(p)
    else if (/\.jsx?$/.test(f)) {
      const s = fs.readFileSync(p, 'utf8')
      for (const m of s.matchAll(/import\s*\{([^}]+)\}\s*from\s*['"][^'"]*api(?:\/index)?(?:\.js)?['"]/g)) {
        for (const name of m[1].split(',')) {
          const n = name.trim().split(/\s+as\s+/)[0].trim()
          if (!n) continue
          if (!used.has(n)) used.set(n, new Set())
          used.get(n).add(p.replace(/\\/g, '/'))
        }
      }
    }
  }
}
walk('src')

const missing = [...used.keys()].filter((k) => !exported.has(k))
if (missing.length === 0) {
  console.log('OK: 所有被引用的 API 函数均已导出')
} else {
  console.log('缺失的导出（需在 api/index.js 中补充）:')
  for (const m of missing) console.log('  -', m, '  ←', [...used.get(m)].join(', '))
}
console.log('\n已导出函数数:', exported.size, ' 被引用函数数:', used.size)
