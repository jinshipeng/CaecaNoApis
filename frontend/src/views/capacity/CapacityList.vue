<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { OfficeBuilding, DataAnalysis, Odometer, WarnTriangleFilled } from '@element-plus/icons-vue'
import { getCapacityList, deleteCapacity, getCapacityStats } from '@/api/capacity'
import { debounce } from '@/utils/debounce'

const loading = ref(false)
const tableData = ref<any[]>([])
const selectedRows = ref<any[]>([])
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: ''
})

const loadData = async () => {
  loading.value = true
  try {
    const res = await getCapacityList({
      page: pagination.value.current,
      page_size: pagination.value.pageSize,
      search: searchForm.value.search,
      ordering: '-id'
    })
    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    console.error('加载产能数据失败:', error)
    tableData.value = []
    pagination.value.total = 0
  } finally {
    loading.value = false
  }
}

const handleSearch = debounce(() => {
  pagination.value.current = 1
  loadData()
}, 300)

const handleReset = () => {
  searchForm.value = { search: '' }
  handleSearch()
}


const handleSizeChange = (val: number) => {
  pagination.value.pageSize = val
  pagination.value.current = 1
  loadData()
}

const handleCurrentChange = (val: number) => {
  pagination.value.current = val
  loadData()
}

const capacityStats = ref({
  totalWorkCenters: 0,
  totalDailyCapacity: 0,
  totalWeeklyCapacity: 0,
  activeCount: 0
})

const loadCapacityStats = async () => {
  try {
    const res = await getCapacityStats()
    capacityStats.value = {
      totalWorkCenters: res.work_center_count || 0,
      totalDailyCapacity: res.total_daily_capacity || 0,
      totalWeeklyCapacity: res.total_weekly_capacity || 0,
      activeCount: res.active_count || 0
    }
  } catch (e) {
    console.error('加载产能统计失败:', e)
  }
}

const capacityBarData = computed(() => {
  const workCenterMap = new Map<string, number>()
  tableData.value.forEach(item => {
    const key = item.work_center
    const current = workCenterMap.get(key) || 0
    workCenterMap.set(key, current + Number(item.daily_capacity || 0))
  })
  const maxCap = Math.max(...Array.from(workCenterMap.values()), 1)
  return Array.from(workCenterMap.entries()).map(([name, capacity]) => ({
    name,
    capacity,
    percentage: Math.round((capacity / maxCap) * 100)
  }))
})

const handleSelectionChange = (rows: any[]) => {
  selectedRows.value = rows
}

const handleBatchDelete = async () => {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedRows.value.length} 条产能数据吗？`,
      '批量删除确认',
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' }
    )
    await Promise.all(selectedRows.value.map(row => deleteCapacity(row.id)))
    ElMessage.success(`成功删除 ${selectedRows.value.length} 条`)
    selectedRows.value = []
    loadData()
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  }
}

onMounted(() => {
  loadData()
  loadCapacityStats()
})
</script>

<template>
  <div class="capacity-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">产能管理</h1>
        <p class="page-desc">监控各工作中心产能配置</p>
      </div>
    </div>

    <el-row :gutter="20" class="stats-row">
      <el-col :xs="24" :sm="6" class="stat-col">
        <div class="stat-card">
          <div class="stat-icon purple">
            <el-icon><OfficeBuilding /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-value">{{ capacityStats.totalWorkCenters }}</div>
            <div class="stat-label">工作中心</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="6" class="stat-col">
        <div class="stat-card">
          <div class="stat-icon blue">
            <el-icon><DataAnalysis /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-value">{{ capacityStats.totalDailyCapacity }}</div>
            <div class="stat-label">总日产能</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="6" class="stat-col">
        <div class="stat-card">
          <div class="stat-icon green">
            <el-icon><Odometer /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-value">{{ capacityStats.totalWeeklyCapacity }}</div>
            <div class="stat-label">总周产能</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="6" class="stat-col">
        <div class="stat-card">
          <div class="stat-icon orange">
            <el-icon><WarnTriangleFilled /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-value">{{ capacityStats.activeCount }}</div>
            <div class="stat-label">启用配置</div>
          </div>
        </div>
      </el-col>
    </el-row>

    <el-card class="table-card" v-if="capacityBarData.length > 0">
      <div class="table-header">
        <h3>工作中心产能分布</h3>
      </div>

      <div class="capacity-chart">
        <div
          v-for="wc in capacityBarData"
          :key="wc.name"
          class="capacity-bar-item"
        >
          <div class="bar-label">{{ wc.name }}</div>
          <div class="bar-container">
            <div
              class="bar-fill"
              :style="{ width: wc.percentage + '%' }"
            >
              <span class="bar-text">{{ wc.capacity }}</span>
            </div>
          </div>
        </div>
      </div>
    </el-card>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="工作中心/产线编号"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="table-card">
      <div class="table-toolbar">
        <span class="selection-info">已选 {{ selectedRows.length }} 项</span>
        <div style="display:flex;gap:8px">
          <el-button type="danger" size="small" :disabled="selectedRows.length === 0" @click="handleBatchDelete">删除选中</el-button>
        </div>
      </div>

      <div class="table-header">
        <h3>产能配置详情</h3>
      </div>

      <el-table border
        :data="tableData"
        :loading="loading"
        class="capacity-table"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="36" />
        <el-table-column prop="work_center" label="工作中心" width="130" show-overflow-tooltip />
        <el-table-column prop="material_code" label="产线编号" width="80" align="center" />
        <el-table-column prop="daily_capacity" label="日产能" width="80" align="center" />
        <el-table-column prop="weekly_capacity" label="周产能" width="80" align="center" />
        <el-table-column label="状态" width="56">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="pagination.current"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[15, 30, 50, 100]"
          :total="pagination.total"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="handleCurrentChange"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.capacity-page {
  max-width: 1500px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 24px;

  .page-title {
    font-size: 28px;
    font-weight: 700;
    color: #e2e8f0;
    margin: 0 0 8px 0;
  }

  .page-desc {
    font-size: 14px;
    color: #909399;
    margin: 0;
  }
}

.stats-row {
  margin-bottom: 24px;
}

.stat-col {
  margin-bottom: 20px;
}

.stat-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 12px;
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;

  &.purple { background: rgba(155, 89, 182, 0.2); color: #9b59b6; }
  &.blue { background: rgba(64, 158, 255, 0.2); color: #409EFF; }
  &.green { background: rgba(103, 194, 58, 0.2); color: #67c23a; }
  &.orange { background: rgba(230, 162, 60, 0.2); color: #e6a23c; }
}

.stat-content {
  flex: 1;

  .stat-value {
    font-size: 28px;
    font-weight: 700;
    color: #e2e8f0;
    line-height: 1.2;
  }

  .stat-label {
    font-size: 14px;
    color: #909399;
    margin-top: 4px;
  }
}

.search-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  margin-bottom: 16px;

  :deep(.el-card__body) {
    padding: 16px 20px;
  }
}

.table-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  margin-bottom: 24px;

  :deep(.el-card__body) {
    padding: 20px;
  }
}

.table-header {
  margin-bottom: 20px;

  h3 {
    font-size: 18px;
    font-weight: 600;
    color: #e2e8f0;
    margin: 0;
  }
}

.capacity-chart {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.capacity-bar-item {
  display: flex;
  align-items: center;
  gap: 16px;

  .bar-label {
    width: 100px;
    color: #e2e8f0;
    font-size: 14px;
    flex-shrink: 0;
  }

  .bar-container {
    flex: 1;
    height: 32px;
    background: rgba(255, 255, 255, 0.1);
    border-radius: 16px;
    overflow: hidden;

    .bar-fill {
      height: 100%;
      border-radius: 16px;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      padding-right: 12px;
      transition: width 0.3s ease;
      background: linear-gradient(90deg, #409EFF, #66b1ff);
      min-width: 40px;

      .bar-text {
        font-size: 12px;
        font-weight: 600;
        color: white;
        white-space: nowrap;
      }
    }
  }
}

.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: rgba(64, 158, 255, 0.06);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.selection-info {
  font-size: 13px;
  color: #8b92a5;
}

.capacity-table {
  :deep(.el-table) {
    background: rgba(255, 255, 255, 0.02) !important;
  }

  :deep(.el-table__header-wrapper) {
    th {
      background: rgba(110, 158, 247, 0.08) !important;
      color: #B0B8C4;
      font-weight: 600;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
  }

  :deep(.el-table__body-wrapper) {
    background: rgba(255, 255, 255, 0.02);

    td {
      color: #E8EAED;
      vertical-align: middle;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
    }

    tr {
      background: rgba(255, 255, 255, 0.02) !important;

      &:hover {
        background: rgba(110, 158, 247, 0.06) !important;
      }
    }
  }
}

.pagination-container {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

@media (max-width: 767px) {
  .capacity-page { max-width: 100%; }

  .page-title {
    font-size: 24px;
  }

  .stat-card {
    padding: 16px;
  }

  .stat-icon {
    width: 40px;
    height: 40px;
    font-size: 20px;
  }

  .stat-value {
    font-size: 24px;
  }

  .capacity-bar-item {
    flex-direction: column;
    align-items: flex-start;

    .bar-label {
      width: 100%;
    }

    .bar-container {
      width: 100%;
    }
  }

  .search-card :deep(.el-form--inline) {
    flex-wrap: wrap;
  }

  .table-toolbar {
    flex-direction: column;
    gap: 8px;
    align-items: flex-start;
  }
}
</style>
