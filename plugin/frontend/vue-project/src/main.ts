import './assets/main.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'

// 初始化深色模式（在应用挂载前）
const initDarkMode = () => {
  const DARK_MODE_KEY = 'neko-dark-mode'
  const saved = localStorage.getItem(DARK_MODE_KEY)
  if (saved !== null) {
    const dark = saved === 'true'
    if (dark) {
      document.documentElement.classList.add('dark')
    }
  } else {
    // 如果没有保存的设置，检查系统偏好
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    if (prefersDark) {
      document.documentElement.classList.add('dark')
    }
  }
}

initDarkMode()
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import en from 'element-plus/dist/locale/en.mjs'
import router from './router'
import { i18n, getLocale } from './i18n'
import App from './App.vue'

console.log('🚀 Starting N.E.K.O Plugin Management System...')

const app = createApp(App)

// 注册所有图标
console.log('📦 Registering Element Plus icons...')
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

console.log('✅ Setting up Pinia...')
app.use(createPinia())

console.log('✅ Setting up Router...')
app.use(router)

console.log('✅ Setting up i18n...')
app.use(i18n)

console.log('✅ Setting up Element Plus...')
// 根据当前语言设置 Element Plus 的 locale
const currentLocale = getLocale()
app.use(ElementPlus, {
  locale: currentLocale === 'zh-CN' ? zhCn : en
})

console.log('✅ Mounting app to #app...')
app.mount('#app')

console.log('✅ App mounted successfully!')
