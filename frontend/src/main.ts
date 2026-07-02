import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './assets/styles/global.scss'

const app = createApp(App)

// 全局错误处理：捕获组件渲染和生命周期中的未处理异常
app.config.errorHandler = (err, _instance, info) => {
  console.error('全局错误:', err)
  console.error('错误来源:', info)
  // 避免在登录页显示错误提示（可能是token过期导致的正常跳转）
  if (window.location.pathname !== '/login') {
    import('element-plus').then(({ ElMessage }) => {
      ElMessage.error('系统发生异常，请刷新页面重试')
    })
  }
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')
