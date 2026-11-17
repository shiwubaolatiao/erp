import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 新系统端口规划（与旧系统并行运行不冲突）：
//   旧系统：后端 8000 / 前端 5173
//   新系统：后端 8020 / 前端 5180
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5180,
    proxy: {
      // 旧系统接口（部分页面过渡期兼容）
      '/api/v2': {
        target: 'http://localhost:8020',
        changeOrigin: true,
        timeout: 600000,
      },
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        timeout: 600000,
      }
    }
  }
})
