<script setup lang="ts">
import { onMounted } from 'vue'
import { useUserStore } from '@/stores/user'
import LoadingOverlay from '@/components/LoadingOverlay.vue'

const userStore = useUserStore()

onMounted(() => {
  userStore.checkAuth()
})
</script>

<template>
  <router-view />
  <LoadingOverlay />
</template>

<style>
#app {
  width: 100%;
  height: 100%;
}

/* ===== 全局表格优化样式 ===== */

/* 表格全宽自适应 */
.el-table {
  width: 100% !important;
}

.el-table .el-table__header-wrapper table,
.el-table .el-table__body-wrapper table {
  width: 100% !important;
}

/* 单元格内容溢出省略（配合 show-overflow-tooltip 使用） */
.el-table .cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 列宽拖拽调整手柄样式 */
.el-table th .cell {
  position: relative;
}

/* 拖拽手柄悬停提示 */
.el-table__header th.is-sortable:hover,
.el-table__header th:not(.is-hidden):hover {
  background-color: rgba(255, 255, 255, 0.04);
}

/* 调整列宽时的视觉反馈 */
.el-table--border .el-table__cell {
  position: relative;
}

/* Tooltip 样式优化 */
.el-popper.is-dark {
  max-width: 400px !important;
  word-break: break-all;
}
</style>