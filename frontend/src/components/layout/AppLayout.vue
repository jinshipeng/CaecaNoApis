<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { ElMessageBox } from 'element-plus'
import {
  Box,
  OfficeBuilding,
  Document,
  DataAnalysis,
  Cpu,
  DataBoard,
  Expand,
  Fold,
  CircleClose
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const isMobileMenuOpen = ref(false)
// 手动折叠状态（非大屏页面使用）
const _manualCollapse = ref(false)

// 当前路径是否为大屏页面
const isScreenPage = computed(() => route.path === '/screen')
// 侧边栏折叠状态：跟随手动状态（进入大屏时自动折叠，离开时自动展开）
const sidebarCollapsed = computed(() => _manualCollapse.value)
// 侧边栏是否应该可见（用于控制移动端的 is-mobile-open 类）
// 未折叠或移动端手动打开时均显示
const sidebarVisible = computed(() => {
  return !sidebarCollapsed.value || isMobileMenuOpen.value
})

const toggleCollapse = () => {
  _manualCollapse.value = !_manualCollapse.value
}
const toggleMobileMenu = () => {
  isMobileMenuOpen.value = !isMobileMenuOpen.value
}
const activeMenu = computed(() => route.path)
const expandedMenus = ref<string[]>([])

const menuItems = [
  { path: '/screen', title: '可视化大屏', icon: DataBoard },
  {
    title: '数据管理',
    icon: Box,
    children: [
      { path: '/material', title: '物料管理' },
      { path: '/bom', title: 'BOM管理' },
      { path: '/inventory', title: '库存管理' }
    ]
  },
  {
    title: '供应链管理',
    icon: OfficeBuilding,
    children: [
      { path: '/supplier', title: '供应商管理' },
      { path: '/purchase', title: '采购订单' }
    ]
  },
  {
    title: '销售订单',
    icon: Document,
    children: [
      { path: '/sales-order', title: '销售订单' },
      { path: '/customer', title: '客户管理' }
    ]
  },
  {
    title: '计划与产能',
    icon: DataAnalysis,
    children: [
      { path: '/material-plan', title: '物料计划' },
      { path: '/capacity', title: '产能管理' }
    ]
  },
  {
    title: '系统',
    icon: Cpu,
    children: [
      { path: '/import', title: '数据导入' },
      { path: '/audit', title: '审计日志' },
      { path: '/help', title: '帮助中心' }
    ]
  }
]

const toggleMenu = (title: string) => {
  // 折叠状态下点击父菜单，自动展开侧边栏
  if (sidebarCollapsed.value) {
    _manualCollapse.value = false
  }
  const index = expandedMenus.value.indexOf(title)
  if (index > -1) {
    expandedMenus.value.splice(index, 1)
  } else {
    expandedMenus.value.push(title)
  }
}

const handleLogout = async () => {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    userStore.logout()
    router.push('/login')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('退出登录失败:', error)
      userStore.logout()
      router.push('/login')
    }
  }
}

// 根据路径找到所属的父菜单标题
const getParentTitle = (path: string): string | null => {
  for (const item of menuItems) {
    if (item.children) {
      for (const child of item.children) {
        if (child.path === path) {
          return item.title
        }
      }
    }
  }
  return null
}

const handleMenuSelect = (path: string) => {
  // 离开大屏页面时自动展开侧边栏
  if (path !== '/screen') {
    _manualCollapse.value = false
  }
  // 保持当前路径所属的父菜单展开，而非清空全部
  const parentTitle = getParentTitle(path)
  if (parentTitle) {
    if (!expandedMenus.value.includes(parentTitle)) {
      expandedMenus.value = [parentTitle]
    }
  }
  router.push({ path: path }).catch(() => {})
}

const currentRouteTitle = computed(() => {
  const titleMap: Record<string, string> = {
    '/screen': '可视化大屏',
    '/material': '物料管理',
    '/bom': 'BOM管理',
    '/inventory': '库存管理',
    '/supplier': '供应商管理',
    '/purchase': '采购订单',
    '/sales-order': '销售订单',
    '/customer': '客户管理',
    '/material-plan': '物料计划',
    '/capacity': '产能管理',
    '/import': '数据导入',
    '/audit': '审计日志',
    '/help': '帮助中心'
  }
  return titleMap[route.path] || '联宝智能'
})

const breadcrumbs = computed(() => {
  const path = route.path
  const map: Record<string, { parent: string; current: string }> = {
    '/material': { parent: '数据管理', current: '物料管理' },
    '/bom': { parent: '数据管理', current: 'BOM管理' },
    '/inventory': { parent: '数据管理', current: '库存管理' },
    '/supplier': { parent: '供应链管理', current: '供应商管理' },
    '/purchase': { parent: '供应链管理', current: '采购订单' },
    '/sales-order': { parent: '销售订单', current: '销售订单' },
    '/customer': { parent: '销售订单', current: '客户管理' },
    '/material-plan': { parent: '计划与产能', current: '物料计划' },
    '/capacity': { parent: '计划与产能', current: '产能管理' },
    '/import': { parent: '系统', current: '数据导入' },
    '/audit': { parent: '系统', current: '审计日志' },
    '/help': { parent: '系统', current: '帮助中心' },
  }
  return map[path] || null
})

// 监听是否在大屏页面：进入大屏时折叠侧边栏，离开时展开
watch(isScreenPage, (isScreen) => {
  _manualCollapse.value = isScreen
  // 清理可能残留的全屏样式类
  document.body.classList.remove('screen-fullscreen')
})

// 组件挂载时确保状态正确
onMounted(() => {
  if (isScreenPage.value) {
    _manualCollapse.value = true
  }
})
</script>

<template>
  <div class="app-layout">
    <aside class="sidebar" :class="{ 'is-collapsed': sidebarCollapsed, 'is-mobile-open': sidebarVisible }">
      <div class="sidebar-header">
        <div class="logo" :class="{ 'collapsed': sidebarCollapsed }">
          <span class="logo-icon"><el-icon><Cpu /></el-icon></span>
          <span class="logo-text" v-if="!sidebarCollapsed">联宝智能</span>
        </div>
      </div>

      <div class="menu-container">
        <template v-for="(item, index) in menuItems" :key="item.path || item.title">
          <button
            v-if="!item.children"
            :class="['menu-item', { 'is-active': activeMenu === item.path }]"
            @click="handleMenuSelect(item.path)"
            type="button"
            :style="{ animationDelay: `${index * 0.05}s` }"
          >
            <span class="menu-icon-wrapper" :class="{ 'active': activeMenu === item.path }">
              <el-icon :size="18"><component :is="item.icon" /></el-icon>
            </span>
            <span class="menu-text">{{ item.title }}</span>
          </button>

          <div v-else class="menu-group">
            <button
              :class="['menu-item menu-item-parent', { 'is-expanded': expandedMenus.includes(item.title) }]"
              @click="toggleMenu(item.title)"
              type="button"
              :style="{ animationDelay: `${index * 0.05}s` }"
            >
              <span class="menu-icon-wrapper" :class="{ 'active': expandedMenus.includes(item.title) }">
                <el-icon :size="18"><component :is="item.icon" /></el-icon>
              </span>
              <span class="menu-text">{{ item.title }}</span>
              <span class="menu-arrow" :class="{ 'rotated': expandedMenus.includes(item.title) }"></span>
            </button>

            <div v-show="expandedMenus.includes(item.title)" class="submenu">
              <button
                v-for="(child, childIndex) in item.children"
                :key="child.path"
                :class="['menu-item menu-item-child', { 'is-active': activeMenu === child.path }]"
                @click="handleMenuSelect(child.path)"
                type="button"
                :style="{ animationDelay: `${index * 0.05 + childIndex * 0.02}s` }"
              >
                <span class="menu-text">{{ child.title }}</span>
              </button>
            </div>
          </div>
        </template>
      </div>

      <div class="sidebar-footer">
        <div class="user-section" :class="{ 'collapsed': sidebarCollapsed }">
          <div class="user-info" v-if="!sidebarCollapsed">
            <span class="user-name">{{ userStore.userInfo?.full_name || '用户' }}</span>
          </div>
          <button
            @click="handleLogout"
            class="logout-btn"
            :class="{ 'collapsed': sidebarCollapsed }"
          >
            <el-icon v-if="sidebarCollapsed" class="logout-icon"><CircleClose /></el-icon>
            <span v-else>退出</span>
          </button>
        </div>
        <button
          @click="toggleCollapse"
          class="collapse-btn"
        >
          <Expand v-if="sidebarCollapsed" :size="16" />
          <Fold v-else :size="16" />
        </button>
      </div>
    </aside>

    <main class="main-content">
      <!-- 面包屑导航 -->
      <el-breadcrumb v-if="breadcrumbs" class="page-breadcrumb" separator="/">
        <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
        <el-breadcrumb-item>{{ breadcrumbs.parent }}</el-breadcrumb-item>
        <el-breadcrumb-item>{{ breadcrumbs.current }}</el-breadcrumb-item>
      </el-breadcrumb>

      <div class="mobile-top-bar">
        <button class="hamburger-btn" @click="toggleMobileMenu" type="button">
          <span>☰</span>
        </button>
        <span class="mobile-title">{{ currentRouteTitle }}</span>
      </div>

      <!-- 移动端遮罩层 -->
      <div
        class="mobile-overlay"
        v-if="isMobileMenuOpen"
        @click="toggleMobileMenu"
      ></div>

      <router-view v-slot="{ Component }">
        <KeepAlive :max="10">
          <component :is="Component" :key="route.path" />
        </KeepAlive>
      </router-view>
    </main>
  </div>
</template>

<style scoped lang="scss">
.app-layout {
  display: flex;
  width: 100%;
  height: 100%;
  background: linear-gradient(135deg, #1F2330 0%, #282C3A 100%);
  overflow: hidden;
}

.sidebar {
  width: 240px;
  height: 100%;
  background: rgba(23, 26, 35, 0.98);
  border-right: 1px solid rgba(255, 255, 255, 0.06);
  display: flex;
  flex-direction: column;
  position: relative;
  transition: width 0.3s ease;
  backdrop-filter: blur(20px);

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: linear-gradient(180deg, rgba(110, 158, 247, 0.03) 0%, transparent 100%);
    pointer-events: none;
  }

  &.is-collapsed {
    width: 64px;

    .sidebar-header {
      padding: 0;
    }

    .menu-container {
      padding: 12px 0;
    }

    .menu-item {
      justify-content: center;
      padding: 10px 0;
      gap: 0;

      &::before { display: none; }

      .menu-text, .menu-arrow {
        display: none;
      }

      .menu-icon-wrapper {
        margin-right: 0;
        margin-left: 0;
      }
    }

    .menu-item-child {
      display: none;
    }
  }
}

.sidebar-header {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  position: relative;
  z-index: 1;

  .logo {
    display: flex;
    align-items: center;
    gap: 10px;
    transition: all 0.3s ease;

    &.collapsed {
      gap: 0;
    }

    .logo-icon {
      font-size: 28px;
      background: linear-gradient(135deg, #6E9EF7, #5DAF5A);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      animation: logoPulse 3s ease-in-out infinite;
    }

    .logo-text {
      font-size: 18px;
      font-weight: 700;
      background: linear-gradient(135deg, #6E9EF7, #5DAF5A);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      transition: opacity 0.3s ease;
    }
  }
}

@keyframes logoPulse {
  0%, 100% {
    transform: scale(1);
    filter: brightness(1);
  }
  50% {
    transform: scale(1.05);
    filter: brightness(1.2);
  }
}

.menu-container {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 16px 8px;
  position: relative;
  z-index: 1;

  &::-webkit-scrollbar {
    width: 4px;
  }

  &::-webkit-scrollbar-thumb {
    background: rgba(144, 147, 153, 0.2);
    border-radius: 2px;
  }
}

.menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  margin-bottom: 4px;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  color: #a8adb5;
  background: transparent;
  border: none;
  text-align: left;
  position: relative;
  overflow: hidden;
  animation: menuItemFadeIn 0.4s ease-out backwards;

  &::before {
    content: '';
    position: absolute;
    left: 0;
    top: 50%;
    transform: translateY(-50%);
    width: 3px;
    height: 0;
    background: linear-gradient(180deg, #6E9EF7, #5DAF5A);
    border-radius: 0 3px 3px 0;
    transition: height 0.3s ease;
  }

  &:hover {
    background: rgba(110, 158, 247, 0.06);
    color: #E8EAED;
    transform: translateX(4px);

    .menu-icon-wrapper {
      background: rgba(110, 158, 247, 0.12);
      color: #6E9EF7;
    }

    .menu-arrow {
      opacity: 1;
      transform: translateX(0);
    }
  }

  &.is-active {
    background: rgba(110, 158, 247, 0.1);
    color: #6E9EF7;

    &::before {
      height: 60%;
    }

    .menu-icon-wrapper {
      background: linear-gradient(135deg, rgba(110, 158, 247, 0.25), rgba(93, 175, 90, 0.25));
      color: #6E9EF7;
      box-shadow: 0 3px 10px rgba(110, 158, 247, 0.2);
    }

    .menu-text {
      font-weight: 600;
    }

    .menu-arrow {
      opacity: 1;
      transform: translateX(0) rotate(180deg);
      border-top-color: #FF6B6B;
    }
  }
}

@keyframes menuItemFadeIn {
  from {
    opacity: 0;
    transform: translateX(-20px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

.menu-icon-wrapper {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  margin-right: 8px;
  background: rgba(255, 255, 255, 0.02);
  transition: all 0.3s ease;
  flex-shrink: 0;
}

.menu-text {
  font-size: 14px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: all 0.3s ease;
}

.menu-arrow {
  width: 0;
  height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 6px solid #E57373;
  opacity: 1;
  transform: translateX(0);
  transition: all 0.3s ease;
  flex-shrink: 0;

  &.rotated {
    transform: rotate(180deg);
    border-top-color: #FF8A80;
  }
}

.menu-group {
  margin-bottom: 4px;
}

.menu-item-parent {
  .menu-arrow {
    opacity: 1;
    transform: translateX(0);
  }

  &.is-expanded {
    background: rgba(110, 158, 247, 0.08);

    .menu-icon-wrapper {
      background: rgba(110, 158, 247, 0.15);
      color: #6E9EF7;
    }
  }
}

.menu-item-child {
  padding-left: 56px;
  font-size: 13px;

  &:hover {
    transform: translateX(2px);
    background: rgba(110, 158, 247, 0.04);

    .menu-text {
      color: #E8EAED;
    }
  }

  &.is-active {
    background: rgba(110, 158, 247, 0.08);
    color: #6E9EF7;

    &::before {
      height: 40%;
    }

    .menu-text {
      font-weight: 500;
    }
  }
}

.submenu {
  overflow: hidden;
  transition: all 0.3s ease;
}

.sidebar-footer {
  padding: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: relative;
  z-index: 1;
}

.user-section {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  transition: all 0.3s ease;

  &.collapsed {
    justify-content: center;
  }

  .user-info {
    flex: 1;

    .user-name {
      font-size: 14px;
      color: #e2e8f0;
      font-weight: 500;
    }
  }
}

.logout-btn {
  background: rgba(245, 108, 108, 0.1);
  border: 1px solid rgba(245, 108, 108, 0.2);
  color: #f56c6c;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.3s ease;
  display: flex;
  align-items: center;
  justify-content: center;

  &:hover {
    background: rgba(245, 108, 108, 0.2);
    transform: translateY(-1px);
  }

  &.collapsed {
    width: 36px;
    height: 36px;
    padding: 0;

    .logout-icon {
      font-size: 16px;
    }
  }
}

.collapse-btn {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: #909399;
  padding: 10px;
  border-radius: 50%;
  cursor: pointer;
  transition: all 0.3s ease;
  align-self: center;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;

  &:hover {
    background: rgba(255, 255, 255, 0.08);
    color: #e2e8f0;
    transform: rotate(180deg);
  }
}

.page-breadcrumb {
  margin-bottom: 12px;
  padding: 4px 0;

  :deep(.el-breadcrumb__inner) {
    color: #909399;
    font-size: 13px;
    transition: color 0.2s;

    &.is-link:hover {
      color: #6E9EF7;
    }
  }

  :deep(.el-breadcrumb__item:last-child .el-breadcrumb__inner) {
    color: #B0B8C4;
    font-weight: 500;
  }

  :deep(.el-breadcrumb__separator) {
    color: #606266;
    margin: 0 6px;
  }
}

.main-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 24px;
  position: relative;

  /* 确保滚动容器层级正确，不被伪元素阻挡 */
  z-index: 0;

  &::-webkit-scrollbar {
    width: 8px;
  }

  &::-webkit-scrollbar-track {
    background: rgba(255, 255, 255, 0.03);
  }

  &::-webkit-scrollbar-thumb {
    background: rgba(144, 147, 153, 0.3);
    border-radius: 4px;

    &:hover {
      background: rgba(144, 147, 153, 0.5);
    }
  }
}

.app-layout {
  --sidebar-width: 240px;

  &:has(.sidebar.is-collapsed) {
    --sidebar-width: 64px;
  }
}

.mobile-top-bar {
  display: none;
}

@media (max-width: 767px) {
  .mobile-top-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 0;
    margin-bottom: 8px;

    .hamburger-btn {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.1);
      color: #e2e8f0;
      font-size: 20px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.2s;

      &:hover {
        background: rgba(110, 158, 247, 0.15);
        color: #6E9EF7;
      }
    }

    .mobile-title {
      font-size: 18px;
      font-weight: 600;
      color: #e2e8f0;
    }
  }

  .mobile-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 99;
  }

  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    z-index: 100;
    transform: translateX(-100%);
    transition: transform 0.3s ease;
    box-shadow: 8px 0 32px rgba(0, 0, 0, 0.3);

    &.is-mobile-open {
      transform: translateX(0);
      width: 240px !important;
    }

    &.is-collapsed {
      transform: translateX(-100%);
    }

    &.is-collapsed.is-mobile-open {
      transform: translateX(0);
      width: 240px !important;
    }
  }

  .page-breadcrumb {
    display: none;
  }

  .main-content {
    padding: 16px;
  }
}
</style>
