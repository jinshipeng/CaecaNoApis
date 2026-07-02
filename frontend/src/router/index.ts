import { createRouter, createWebHistory, RouteRecordRaw } from 'vue-router'
import AppLayout from '@/components/layout/AppLayout.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/auth/Login.vue'),
    meta: { requiresAuth: false }
  },
  {
    path: '/',
    component: AppLayout,
    redirect: '/screen',
    meta: { requiresAuth: true },
    children: [
      {
        path: 'screen',
        name: 'Screen',
        component: () => import('@/views/screen/VisualScreen.vue'),
        meta: { title: '可视化大屏', icon: 'DataBoard' }
      },
      {
        path: 'material',
        name: 'Material',
        component: () => import('@/views/material/MaterialList.vue'),
        meta: { title: '物料管理', icon: 'Box' }
      },
      {
        path: 'customer',
        name: 'Customer',
        component: () => import('@/views/customer/CustomerList.vue'),
        meta: { title: '客户管理', icon: 'User' }
      },
      {
        path: 'supplier',
        name: 'Supplier',
        component: () => import('@/views/supplier/SupplierList.vue'),
        meta: { title: '供应商管理', icon: 'OfficeBuilding' }
      },
      {
        path: 'bom',
        name: 'BOM',
        component: () => import('@/views/bom/BOMList.vue'),
        meta: { title: 'BOM管理', icon: 'Tickets' }
      },
      {
        path: 'sales-order',
        name: 'SalesOrder',
        component: () => import('@/views/order/SalesOrderList.vue'),
        meta: { title: '销售订单', icon: 'Document' }
      },
      {
        path: 'material-plan',
        name: 'MaterialPlan',
        component: () => import('@/views/plan/MaterialPlan.vue'),
        meta: { title: '物料计划', icon: 'DataAnalysis' }
      },
      {
        path: 'inventory',
        name: 'Inventory',
        component: () => import('@/views/inventory/InventoryList.vue'),
        meta: { title: '库存管理', icon: 'Goods' }
      },
      {
        path: 'purchase',
        name: 'Purchase',
        component: () => import('@/views/purchase/PurchaseList.vue'),
        meta: { title: '采购订单', icon: 'ShoppingCart' }
      },
      {
        path: 'capacity',
        name: 'Capacity',
        component: () => import('@/views/capacity/CapacityList.vue'),
        meta: { title: '产能管理', icon: 'Cpu' }
      },
      {
        path: 'import',
        name: 'Import',
        component: () => import('@/views/system/DataImport.vue'),
        meta: { title: '数据导入', icon: 'Upload' }
      },
      {
        path: 'audit',
        name: 'Audit',
        component: () => import('@/views/system/AuditLog.vue'),
        meta: { title: '审计日志', icon: 'List' }
      },
      {
        path: 'help',
        name: 'Help',
        component: () => import('@/views/system/HelpCenter.vue'),
        meta: { title: '帮助中心', icon: 'QuestionFilled' }
      },
      {
        path: 'ai-analysis',
        redirect: '/screen?tab=ai-analysis'
      },
      {
        path: 'digital-twin',
        redirect: '/screen?tab=digital-twin'
      },
    ]
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/screen'
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

function isValidTokenFormat(token: string): boolean {
  if (!token || token.length < 10) return false

  const jwtRegex = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$/
  if (jwtRegex.test(token)) return true

  const djangoTokenRegex = /^[a-f0-9]{40}$/
  if (djangoTokenRegex.test(token)) return true

  return token.length >= 20 && /^[A-Za-z0-9]+$/.test(token)
}

function isTokenExpired(token: string): boolean {
  const jwtRegex = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$/
  if (!jwtRegex.test(token)) return false

  try {
    const payload = token.split('.')[1]
    if (!payload) return true
    const decodedPayload = JSON.parse(atob(payload))
    if (!decodedPayload.exp) return false
    const currentTime = Math.floor(Date.now() / 1000)
    return decodedPayload.exp < currentTime
  } catch {
    return true
  }
}

function clearAuthData(): void {
  localStorage.removeItem('token')
  localStorage.removeItem('userInfo')
}

router.beforeEach((to, _from, next) => {
  const token = localStorage.getItem('token')

  if (to.meta.requiresAuth !== false) {
    if (!token) {
      next('/login')
      return
    }

    if (!isValidTokenFormat(token)) {
      console.warn('Token format invalid, clearing auth data')
      clearAuthData()
      next('/login')
      return
    }

    if (isTokenExpired(token)) {
      console.warn('Token expired, clearing auth data')
      clearAuthData()
      next('/login')
      return
    }

    next()
  } else {
    if (token && to.path === '/login') {
      next('/')
      return
    }
    next()
  }
})

router.afterEach((_to, _from) => {
  window.scrollTo(0, 0)
})

window.addEventListener('beforeunload', () => {
  const sensitiveData = ['password', 'secret', 'credential']
  for (const key of Object.keys(localStorage)) {
    if (sensitiveData.some(sensitive => key.toLowerCase().includes(sensitive))) {
      localStorage.removeItem(key)
    }
  }
})

export default router
