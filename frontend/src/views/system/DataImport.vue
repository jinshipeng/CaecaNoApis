<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { ElMessage, ElUpload, ElMessageBox } from 'element-plus'
import { Upload, Check, Document, Close, Delete, MagicStick, Clock, Refresh, Loading } from '@element-plus/icons-vue'
import type { UploadInstance, UploadFile } from 'element-plus'
import { service } from '@/api/request'
import { getImportHistory } from '@/api'

const loading = ref(false)
const uploadRef = ref<UploadInstance>()
const importHistory = ref<Array<{
  id: number
  type: string
  filename: string
  status: string
  count: number
  time: string
}>>([])

const fileType = ref('auto')  // 默认选择"自动检测"
const cleanImport = ref(false)
const autoDetectedInfo = ref<any>(null)
const isDetecting = ref(false)

// 多文件支持
const selectedFiles = ref<UploadFile[]>([])
const batchResults = ref<Array<{
  filename: string
  status: 'pending' | 'importing' | 'success' | 'error'
  type?: string
  count?: number
  message?: string
}>>([])
const isBatchMode = ref(false)

const fileTypes = [
  { label: '自动检测（推荐）', value: 'auto', desc: '系统自动识别CSV类型并智能分组处理' },
  { label: '物料数据', value: 'material', desc: '原材料、半成品、成品基础信息' },
  { label: '供应商数据', value: 'supplier', desc: '供应商/厂商信息' },
  { label: 'BOM数据（成品组成）', value: 'bom', desc: '成品与原材料的组成关系' },
  { label: '库存数据', value: 'inventory', desc: '当前库存数量和状态' },
  { label: '订单数据', value: 'order', desc: '销售订单信息' },
  { label: '采购订单', value: 'purchase', desc: '采购订单/供应商交货信息' },
  { label: '工作中心/产线', value: 'workcenter', desc: '生产线、工作站配置' },
  { label: '客户数据', value: 'customer', desc: '客户基础信息' },
  { label: '系统配置数据', value: 'config', desc: '工厂日历/调拨记录/优先级规则' }
]

const currentTypeDesc = ref('')

watch(fileType, (newVal) => {
  const typeInfo = fileTypes.find(t => t.value === newVal)
  currentTypeDesc.value = typeInfo?.desc || ''
}, { immediate: true })

const selectedFile = ref<UploadFile | null>(null)

// 智能检测CSV类型（纯前端，不发请求到后端）
const detectFileType = async (file: UploadFile) => {
  if (!file || !file.raw) return

  isDetecting.value = true
  autoDetectedInfo.value = null

  try {
    // 纯前端读取CSV列名进行类型推断，不发送到后端
    const text = await readFileAsText(file.raw)
    const lines = text.split('\n').filter(line => line.trim())
    if (lines.length < 2) return

    // 提取列名
    const columns = lines[0].split(',').map(col => col.trim().replace(/^"|"$/g, ''))

    // 前端本地匹配规则（与后端 auto_detect_data_type 逻辑对齐）
    const colSet = new Set(columns.map(c => c.toLowerCase()))
    let detectedType = 'unknown'
    let confidence = 0.3

    // 基于首列特征优先判断（避免误检）
    const firstCol = (columns[0] || '').toLowerCase().trim()

    if (firstCol === '物料id' || firstCol === '物料代码' || firstCol === '物料编号') {
      detectedType = 'material'; confidence = 0.99
    } else if (firstCol === '采购订单号' || firstCol === '采购单号' || firstCol === '采购订单id') {
      detectedType = 'purchase'; confidence = 0.99
    } else if (firstCol === '客户id' || firstCol === '客户代码' || firstCol === '客户编号') {
      detectedType = 'customer'; confidence = 0.99
    } else if (colSet.has('物料id') || colSet.has('material_code') || colSet.has('物料代码')) {
      detectedType = 'material'; confidence = 0.95
    } else if (colSet.has('成品id') || colSet.has('构成原材料id') || colSet.has('父项物料')) {
      detectedType = 'bom'; confidence = 0.95
    } else if (colSet.has('供应商id') || colSet.has('供应商代码') || colSet.has('supplier_code') || colSet.has('供应商编号')) {
      detectedType = 'supplier'; confidence = 0.92
    } else if (colSet.has('客户id') || colSet.has('客户代码') || colSet.has('customer_code') || colSet.has('客户编号')) {
      detectedType = 'customer'; confidence = 0.92
    } else if (colSet.has('订单id') || colSet.has('订单号') || colSet.has('order_no') || colSet.has('销售订单号')) {
      detectedType = 'order'; confidence = 0.90
    } else if (colSet.has('采购订单号') || colSet.has('po_no') || colSet.has('采购单号')) {
      detectedType = 'purchase'; confidence = 0.90
    } else if (colSet.has('产线id') || colSet.has('产线名称') || colSet.has('workcenter_name') || colSet.has('线体名称')) {
      detectedType = 'workcenter'; confidence = 0.88
    } else if (colSet.has('在库数量') || colSet.has('库存数量') || colSet.has('hold数量') || colSet.has('物料id')) {
      detectedType = 'inventory'; confidence = 0.85
    } else if (colSet.has('数据类型') && (colSet.has('日期') || colSet.has('规则名称') || colSet.has('调拨编号'))) {
      detectedType = 'config'; confidence = 0.80
    }

    if (detectedType !== 'unknown' && confidence > 0.5) {
      autoDetectedInfo.value = { type: detectedType, confidence }
      if (fileType.value === 'auto') {
        ElMessage.success(`已识别为: ${getDetectedTypeName(autoDetectedInfo.value)}`)
      }
    }
  } catch (error) {
    console.error('文件类型检测失败:', error)
  } finally {
    isDetecting.value = false
  }
}

// 辅助函数：读取文件内容
const readFileAsText = (file: File): Promise<string> => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = (e) => resolve(e.target?.result as string)
    reader.onerror = () => reject(new Error('文件读取失败'))
    reader.readAsText(file, 'UTF-8')
  })
}

// 辅助函数：获取检测结果的友好名称
const getDetectedTypeName = (detected: any): string => {
  if (!detected) return '-'

  if (detected.config_sub_type) {
    const subTypeMap: Record<string, string> = {
      'factory_calendar': '工厂日历',
      'factory_transfer': '工厂调拨',
      'priority_rule': '优先级规则',
      'mixed': '混合配置数据'
    }
    return `系统配置 - ${subTypeMap[detected.config_sub_type] || detected.config_sub_type}`
  }

  const typeMap: Record<string, string> = {
    'material': '物料数据',
    'supplier': '供应商数据',
    'customer': '客户数据',
    'bom': 'BOM数据',
    'inventory': '库存数据',
    'order': '订单数据',
    'purchase': '采购订单',
    'workcenter': '工作中心/产线',
    'config': '系统配置'
  }

  return typeMap[detected.type] || '未识别'
}

const handleUpload = (file: UploadFile) => {
  selectedFile.value = file

  // 支持多选模式
  if (!selectedFiles.value.find(f => f.name === file.name)) {
    selectedFiles.value.push(file)
    isBatchMode.value = selectedFiles.value.length > 1
  }

  // 自动触发智能检测（异步执行，不阻塞界面）
  detectFileType(file)
}

const loadImportHistory = async () => {
  try {
    const res: any = await getImportHistory()
    importHistory.value = res?.results || []
  } catch (error) {
    console.error('加载导入历史失败:', error)
    importHistory.value = []
  }
}

const submitImport = async () => {
  if (!selectedFile.value || !selectedFile.value.raw) {
    ElMessage.warning('请先选择要导入的文件')
    return
  }

  loading.value = true

  try {
    const formData = new FormData()
    formData.append('csv_file', selectedFile.value.raw)

    // 如果是自动检测模式，不传import_type或传'auto'
    if (fileType.value !== 'auto') {
      formData.append('import_type', fileType.value)
    }
    formData.append('clean_import', cleanImport.value ? 'true' : 'false')

    // 单文件导入使用2分钟超时（处理大文件）
    const response: any = await service.post('/import_data/', formData, {
      timeout: 120000,
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })

    // 更新检测信息（如果后端返回了）
    if (response.auto_detected) {
      autoDetectedInfo.value = response.auto_detected
    }

    if (response.status === 'success' || response.status === 'partial') {
      const typeLabel = fileTypes.find(t => t.value === fileType.value)?.label ||
                       (response.auto_detected ? getDetectedTypeName(response.auto_detected) : '未识别')
      const totalCount = (response.imported || 0) + (response.updated || 0)
      const autoCreated = response.auto_created || 0

      let successMessage = `文件导入成功！共导入 ${totalCount} 条数据`
      if (autoCreated > 0) {
        successMessage += `（系统自动创建了 ${autoCreated} 条关联数据）`
      }

      // 如果有智能检测结果，显示详细信息
      if (response.auto_detected && response.auto_detected.config_sub_type) {
        const subType = response.auto_detected.config_sub_type
        const subTypeNames: Record<string, string> = {
          'mixed': '混合配置数据',
          'factory_calendar': '工厂日历',
          'factory_transfer': '工厂调拨',
          'priority_rule': '优先级规则'
        }
        successMessage += `\n已识别为: ${subTypeNames[subType] || subType}`
      }

      const newRecord = {
        id: Date.now(),
        type: typeLabel,
        filename: selectedFile.value.name,
        status: response.status,
        count: totalCount,
        time: new Date().toLocaleString('zh-CN')
      }

      importHistory.value.unshift(newRecord)

      if (response.status === 'partial' && response.errors && response.errors.length > 0) {
        const showErrors = async () => {
          const errorText = response.errors.slice(0, 20).join('\n')
          const hasMore = response.errors.length > 20
          await ElMessageBox.alert(
            `${errorText}${hasMore ? `\n\n... 还有 ${response.errors.length - 20} 条错误未显示` : ''}`,
            `导入失败详情（共 ${response.errors.length} 条）`,
            {
              dangerouslyUseHTMLString: false,
              confirmButtonText: '确定',
              type: 'warning'
            }
          )
        }
        ElMessage.warning(`导入完成，但有 ${response.errors.length} 行数据导入失败，点击查看详情`)
        setTimeout(showErrors, 500)
      } else {
        ElMessage.success(successMessage)
      }

      // 注意：不在此处刷新，统一由 finally 块处理（避免重复请求竞态）
    } else {
      ElMessage.error(response.message || '导入失败')
    }
  } catch (error) {
    ElMessage.error('导入失败，请检查网络连接或文件格式')
  } finally {
    // 统一在此处刷新导入历史（无论成功/失败/异常都执行，且只执行一次）
    try {
      await loadImportHistory()
    } catch (e) {
      // 刷新失败不影响主流程
    }

    loading.value = false

    if (uploadRef.value) {
      uploadRef.value.clearFiles()
    }
    selectedFile.value = null
    // 不清除检测结果，让用户可以看到
  }
}

// 批量导入多个文件（顺序执行，按依赖排序）
const submitBatchImport = async () => {
  if (selectedFiles.value.length === 0) {
    ElMessage.warning('请先选择要导入的文件')
    return
  }

  loading.value = true
  batchResults.value = selectedFiles.value.map(file => ({
    filename: file.name,
    status: 'pending' as const
  }))

  let successCount = 0
  let failCount = 0

  try {

  // ===== 导入依赖顺序（数字越小越先导入）=====
  // 物料/供应商/客户/产线 无依赖 → 先导入
  // 库存/BOM/订单/采购 依赖上述 → 后导入
  // 系统配置 最后导入

  // 根据文件名推断类型优先级（用于排序）
  const getFilePriority = (filename: string): number => {
    const name = filename.toLowerCase()
    if (name.includes('物料') || name.includes('material')) return 1
    if (name.includes('供应商') || name.includes('supplier')) return 2
    if (name.includes('客户') || name.includes('customer')) return 3
    if (name.includes('产线') || name.includes('workcenter') || name.includes('产能')) return 4
    if (name.includes('库存') || name.includes('inventory')) return 5
    if (name.includes('bom')) return 6
    if (name.includes('订单') && !name.includes('采购')) return 7
    if (name.includes('采购') || name.includes('purchase')) return 8
    if (name.includes('配置') || name.includes('config')) return 9
    return 5 // 默认中间位置
  }

  // 按依赖顺序排序文件
  const sortedFiles = [...selectedFiles.value].sort((a, b) => {
    return getFilePriority(a.name) - getFilePriority(b.name)
  })

  // 重建 batchResults 以匹配新顺序
  const fileIndexMap = new Map<string, number>()
  sortedFiles.forEach((f, i) => fileIndexMap.set(f.name, i))
  batchResults.value = sortedFiles.map(f => ({
    filename: f.name,
    status: 'pending' as const
  }))

  // ===== 逐个顺序执行（避免SQLite并发锁冲突）=====
  for (let i = 0; i < sortedFiles.length; i++) {
    const file = sortedFiles[i]
    const actualIndex = fileIndexMap.get(file.name) ?? i
    batchResults.value[actualIndex].status = 'importing'

    try {
      const formData = new FormData()
      formData.append('csv_file', file.raw as File)

      if (fileType.value !== 'auto') {
        formData.append('import_type', fileType.value)
      }
      formData.append('clean_import', cleanImport.value ? 'true' : 'false')

      const response: any = await service.post('/import_data/', formData, {
        timeout: 120000,
        headers: { 'Content-Type': 'multipart/form-data' }
      })

      if (response.status === 'success' || response.status === 'partial') {
        const totalCount = (response.imported || 0) + (response.updated || 0)
        batchResults.value[actualIndex] = {
          ...batchResults.value[actualIndex],
          status: 'success',
          type: getDetectedTypeName(response.auto_detected),
          count: totalCount,
          message: response.performance ? `${totalCount}条 · ${response.performance.elapsed_seconds}秒` : `${totalCount}条`
        }
        successCount++
      } else {
        batchResults.value[actualIndex] = {
          ...batchResults.value[actualIndex],
          status: 'error',
          message: response.message || '导入失败'
        }
        failCount++
      }
    } catch (error) {
      batchResults.value[actualIndex] = {
        ...batchResults.value[actualIndex],
        status: 'error',
        message: '网络错误或服务器异常'
      }
      failCount++
    }
  }

  // 显示批量导入结果汇总
  if (failCount === 0) {
    ElMessage.success(`批量导入完成！${successCount} 个文件全部成功`)
  } else if (successCount > 0) {
    ElMessage.warning(`批量导入完成！成功 ${successCount} 个，失败 ${failCount} 个`)
  } else {
    ElMessage.error(`批量导入失败！${failCount} 个文件全部导入失败`)
  }

  // 注意：不在此处刷新，统一由 finally 块处理

  } catch (error) {
    console.error('批量导入异常:', error)
    ElMessage.error('批量导入过程中发生异常，请重试')
  } finally {
    // 统一在此处刷新导入历史（只执行一次）
    try {
      await loadImportHistory()
    } catch (e) {
      // 刷新失败不影响主流程
    }

    loading.value = false

    if (uploadRef.value) {
      uploadRef.value.clearFiles()
    }
    selectedFiles.value = []
    isBatchMode.value = false
  }
}

// 移除选中的文件
const removeSelectedFile = (filename: string) => {
  selectedFiles.value = selectedFiles.value.filter(f => f.name !== filename)
  if (selectedFiles.value.length <= 1) {
    isBatchMode.value = false
  }
  if (selectedFile.value?.name === filename) {
    selectedFile.value = selectedFiles.value[0] || null
  }
}

// 清空所有选中文件
const clearAllFiles = () => {
  selectedFiles.value = []
  selectedFile.value = null
  isBatchMode.value = false
  batchResults.value = []
  autoDetectedInfo.value = null
  if (uploadRef.value) {
    uploadRef.value.clearFiles()
  }
}

// 文件移除处理（el-upload组件触发）
const handleRemove = (file: UploadFile) => {
  removeSelectedFile(file.name)
}

// 格式化文件大小
const formatFileSize = (bytes: number): string => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

onMounted(() => {
  loading.value = false
  // 从API加载导入历史记录
  loadImportHistory()
})
</script>

<template>
  <div class="import-page">
    <!-- 页面标题栏（紧凑） -->
    <div class="page-header">
      <div class="header-left">
        <div class="header-icon-wrap">
          <el-icon :size="22"><Upload /></el-icon>
        </div>
        <div>
          <h1 class="page-title">数据导入中心</h1>
          <p class="page-desc">支持CSV/Excel格式，智能识别数据类型，一键批量导入</p>
        </div>
      </div>
    </div>

    <!-- 主内容区：双栏布局 -->
    <div class="import-layout">

      <!-- 左侧：导入操作区 -->
      <div class="upload-section">
        <el-card class="upload-card" shadow="never">
          <template #header>
            <div class="card-header">
              <el-icon><Upload /></el-icon>
              <span>文件上传</span>
            </div>
          </template>

          <!-- 类型选择 + 选项 -->
          <div class="form-row">
            <div class="type-select-wrap">
              <label class="form-label">数据类型</label>
              <el-select v-model="fileType" style="width: 100%" size="default" placeholder="选择数据类型">
                <el-option v-for="type in fileTypes" :key="type.value" :label="type.label" :value="type.value" />
              </el-select>
              <p class="type-desc" v-if="currentTypeDesc">{{ currentTypeDesc }}</p>
            </div>
            <div class="option-wrap">
              <el-checkbox v-model="cleanImport">
                <span class="checkbox-label">清空旧数据重导</span>
              </el-checkbox>
            </div>
          </div>

          <!-- 上传拖拽区 -->
          <el-upload
            v-if="selectedFiles.length === 0"
            ref="uploadRef"
            class="upload-area"
            drag
            multiple
            action="#"
            :auto-upload="false"
            :on-change="handleUpload"
            :on-remove="handleRemove"
            accept=".csv,.xlsx,.xls"
          >
            <div class="upload-content">
              <div class="upload-icon-circle">
                <el-icon :size="32"><Upload /></el-icon>
              </div>
              <p class="upload-main-text">点击或拖拽文件到此处</p>
              <p class="upload-sub-text">支持 CSV / XLSX / XLS 格式，可同时选择多个文件</p>
            </div>
          </el-upload>

          <!-- 已选文件列表 -->
          <transition name="slide-fade">
            <div v-if="selectedFiles.length > 0" class="selected-files-panel">
              <div class="files-panel-header">
                <div class="files-count">
                  <el-icon><Document /></el-icon>
                  <span>已选择 <strong>{{ selectedFiles.length }}</strong> 个文件</span>
                </div>
                <div class="files-panel-actions">
                  <el-upload
                    ref="uploadMoreRef"
                    action="#"
                    :auto-upload="false"
                    :on-change="handleUpload"
                    :show-file-list="false"
                    multiple
                    accept=".csv,.xlsx,.xls"
                    style="display: inline-block;"
                  >
                    <el-button link type="primary" size="small">
                      <el-icon><Upload /></el-icon> 添加文件
                    </el-button>
                  </el-upload>
                  <el-button link type="danger" size="small" @click="clearAllFiles">
                    <el-icon><Delete /></el-icon> 清空全部
                  </el-button>
                </div>
              </div>
              <div class="files-list">
                <div v-for="file in selectedFiles" :key="file.name" class="file-item">
                  <div class="file-item-icon">
                    <el-icon :size="16"><Document /></el-icon>
                  </div>
                  <div class="file-item-info">
                    <span class="file-item-name" :title="file.name">{{ file.name }}</span>
                    <span class="file-item-size">{{ formatFileSize(file.size || 0) }}</span>
                  </div>
                  <button class="file-item-remove" @click="removeSelectedFile(file.name)">
                    <el-icon :size="14"><Close /></el-icon>
                  </button>
                </div>
              </div>

              <!-- 智能检测结果 -->
              <transition name="slide-fade">
                <div v-if="autoDetectedInfo" class="detect-result">
                  <div class="detect-icon">
                    <el-icon :size="16"><MagicStick /></el-icon>
                  </div>
                  <span class="detect-text">
                    智能识别为: <strong>{{ getDetectedTypeName(autoDetectedInfo) }}</strong>
                  </span>
                  <el-tag
                    :type="(autoDetectedInfo.confidence || autoDetectedInfo.sub_confidence || 0) > 0.8 ? 'success' : (autoDetectedInfo.confidence || autoDetectedInfo.sub_confidence || 0) > 0.5 ? 'warning' : 'info'"
                    size="small"
                    effect="dark"
                  >
                    置信度 {{ ((autoDetectedInfo.confidence || autoDetectedInfo.sub_confidence || 0) * 100).toFixed(0) }}%
                  </el-tag>
                </div>
              </transition>

              <!-- 操作按钮 -->
              <div class="action-buttons">
                <el-button
                  v-if="isBatchMode"
                  type="primary"
                  size="large"
                  @click="submitBatchImport"
                  :loading="loading"
                  :disabled="isDetecting || selectedFiles.length === 0"
                  class="import-btn"
                >
                  <el-icon v-if="!loading"><Check /></el-icon>
                  {{ loading ? '批量导入中...' : `批量导入全部 (${selectedFiles.length}个文件)` }}
                </el-button>
                <el-button
                  v-else
                  type="primary"
                  size="large"
                  @click="submitImport"
                  :loading="loading"
                  :disabled="isDetecting || !selectedFile"
                  class="import-btn"
                >
                  <el-icon v-if="!loading"><Check /></el-icon>
                  {{ isDetecting ? '智能检测中...' : '开始导入' }}
                </el-button>
              </div>
            </div>
          </transition>

          <!-- 批量进度 -->
          <transition name="slide-fade">
            <div v-if="batchResults.length > 0 && loading" class="batch-progress">
              <div class="progress-header">
                <el-icon><Loading /></el-icon>
                <span>导入进度</span>
                <el-progress
                  :percentage="Math.round(batchResults.filter(r => r.status === 'success' || r.status === 'error').length / batchResults.length * 100)"
                  :stroke-width="6"
                  style="flex: 1; margin-left: 12px;"
                />
              </div>
              <div class="progress-items">
                <div v-for="(result, index) in batchResults" :key="index" class="progress-item" :class="'status-' + result.status">
                  <div class="progress-item-status">
                    <el-icon v-if="result.status === 'success'" :size="14" class="icon-success"><Check /></el-icon>
                    <el-icon v-else-if="result.status === 'error'" :size="14" class="icon-error"><Close /></el-icon>
                    <el-icon v-else-if="result.status === 'importing'" :size="14" class="icon-importing"><Loading /></el-icon>
                    <el-icon v-else :size="14" class="icon-pending"><Clock /></el-icon>
                  </div>
                  <span class="progress-item-name">{{ result.filename }}</span>
                  <span class="progress-item-info">
                    <template v-if="result.status === 'success'">{{ result.count }}条</template>
                    <template v-else-if="result.status === 'error'">{{ result.message }}</template>
                    <template v-else-if="result.status === 'importing'">导入中...</template>
                    <template v-else>等待中</template>
                  </span>
                </div>
              </div>
            </div>
          </transition>
        </el-card>
      </div>

      <!-- 右侧：导入历史 -->
      <div class="history-section">
        <el-card class="history-card" shadow="never">
          <template #header>
            <div class="card-header">
              <el-icon><Clock /></el-icon>
              <span>导入历史</span>
              <div class="history-stats" v-if="importHistory.length > 0">
                <span class="h-stat"><em>{{ importHistory.length }}</em>累计</span>
                <span class="h-stat success"><em>{{ importHistory.filter(h => h.status === 'success' || h.status === '导入成功').length }}</em>成功</span>
              </div>
              <el-button link type="primary" size="small" @click="loadImportHistory">
                <el-icon><Refresh /></el-icon> 刷新
              </el-button>
            </div>
          </template>
          <el-table
            :data="importHistory"
            stripe
            style="width: 100%"
            :loading="loading"
            empty-text="暂无导入记录"
            size="small"
            max-height="600"
            table-layout="fixed"
          >
            <el-table-column prop="filename" label="文件名" min-width="140" show-overflow-tooltip />
            <el-table-column prop="type" label="类型" width="105" show-overflow-tooltip />
            <el-table-column prop="count" label="条数" width="60" align="center">
              <template #default="{ row }">
                <span class="count-value">{{ row.count || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="time" label="时间" width="155" show-overflow-tooltip />
            <el-table-column label="状态" width="72" align="center">
              <template #default="{ row }">
                <el-tag
                  :type="row.status === 'success' || row.status === '导入成功' ? 'success' : row.status === 'partial' || row.status === '部分成功' ? 'warning' : 'danger'"
                  size="small"
                  effect="dark"
                  round
                >
                  {{ row.status === 'success' || row.status === '导入成功' ? '成功' : row.status === 'partial' || row.status === '部分成功' ? '部分' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>

    </div>
  </div>
</template>

<style scoped>
.import-page {
  padding: 20px;
  min-height: calc(100vh - 84px);
}

/* ===== 页面标题栏（紧凑） ===== */
.page-header {
  display: flex;
  align-items: center;
  margin-bottom: 16px;
  padding: 12px 20px;
  background: rgba(30, 35, 50, 0.85);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 10px;
  backdrop-filter: blur(10px);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-icon-wrap {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: linear-gradient(135deg, #409eff, #6366f1);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex-shrink: 0;
}

.page-title {
  font-size: 18px;
  font-weight: 700;
  color: #e4e7ed;
  margin: 0 0 2px 0;
}

.page-desc {
  font-size: 12px;
  color: #8b92a5;
  margin: 0;
}

/* ===== 双栏布局 ===== */
.import-layout {
  display: flex;
  gap: 16px;
  align-items: stretch;
}

.upload-section {
  flex: 1;
  min-width: 0;
}

.history-section {
  width: 560px;
  flex-shrink: 0;
}

/* ===== 卡片通用 ===== */
.upload-card,
.history-card {
  border-radius: 12px;
  background: rgba(30, 35, 50, 0.85);
  border: 1px solid rgba(255, 255, 255, 0.06);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
  backdrop-filter: blur(10px);
}

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: #e4e7ed;
}

/* ===== 历史卡片统计数字 ===== */
.history-stats {
  display: flex;
  gap: 12px;
  margin-left: auto;
  margin-right: 8px;
}

.h-stat {
  font-size: 11px;
  color: #8b92a5;
  line-height: 1;
}

.h-stat em {
  font-style: normal;
  font-size: 15px;
  font-weight: 700;
  color: #409eff;
  margin-right: 2px;
}

.h-stat.success em {
  color: #67c23a;
}

/* ===== 表单区域 ===== */
.form-row {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 20px;
}

.type-select-wrap {
  flex: 1;
}

.form-label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: #a0a5b8;
  margin-bottom: 6px;
}

.type-desc {
  font-size: 12px;
  color: #6b7084;
  margin: 6px 0 0 0;
}

.option-wrap {
  display: flex;
  align-items: flex-end;
  padding-top: 22px;
}

.checkbox-label {
  color: #a0a5b8;
  font-size: 13px;
}

/* ===== 上传区域 ===== */
.upload-area {
  width: 100%;
}

:deep(.upload-area .el-upload) {
  width: 100%;
}

:deep(.upload-area .el-upload-dragger) {
  width: 100%;
  height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px dashed rgba(255, 255, 255, 0.12);
  border-radius: 12px;
  background: rgba(18, 22, 32, 0.5);
  transition: all 0.3s ease;
}

:deep(.upload-area .el-upload-dragger:hover) {
  border-color: #409eff;
  background: rgba(64, 158, 255, 0.06);
}

.upload-content {
  text-align: center;
}

.upload-icon-circle {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: rgba(64, 158, 255, 0.1);
  border: 1px solid rgba(64, 158, 255, 0.2);
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 12px;
  color: #409eff;
  transition: all 0.3s;
}

:deep(.upload-area .el-upload-dragger:hover) .upload-icon-circle {
  background: rgba(64, 158, 255, 0.15);
  transform: scale(1.05);
}

.upload-main-text {
  font-size: 15px;
  color: #c0c4cc;
  margin: 0 0 6px 0;
  font-weight: 500;
}

.upload-sub-text {
  font-size: 12px;
  color: #6b7084;
  margin: 0;
}

/* ===== 已选文件列表 ===== */
.selected-files-panel {
  margin-top: 16px;
  padding: 14px;
  background: rgba(64, 158, 255, 0.04);
  border: 1px solid rgba(64, 158, 255, 0.1);
  border-radius: 10px;
}

.files-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.files-panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.files-count {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #409eff;
  font-weight: 500;
}

.files-count strong {
  color: #6366f1;
}

.files-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 150px;
  overflow-y: auto;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 8px;
  transition: all 0.2s;
}

.file-item:hover {
  background: rgba(64, 158, 255, 0.06);
  border-color: rgba(64, 158, 255, 0.15);
}

.file-item-icon {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  background: rgba(64, 158, 255, 0.1);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #409eff;
  flex-shrink: 0;
}

.file-item-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.file-item-name {
  font-size: 13px;
  color: #c0c4cc;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-item-size {
  font-size: 11px;
  color: #6b7084;
}

.file-item-remove {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: #6b7084;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  flex-shrink: 0;
}

.file-item-remove:hover {
  background: rgba(245, 108, 108, 0.15);
  color: #f56c6c;
}

/* ===== 智能检测结果 ===== */
.detect-result {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: rgba(103, 194, 58, 0.06);
  border: 1px solid rgba(103, 194, 58, 0.15);
  border-radius: 8px;
}

.detect-icon {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: rgba(103, 194, 58, 0.12);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #67c23a;
  flex-shrink: 0;
}

.detect-text {
  font-size: 13px;
  color: #a0a5b8;
}

.detect-text strong {
  color: #67c23a;
}

/* ===== 操作按钮 ===== */
.action-buttons {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.import-btn {
  width: 100%;
  height: 44px;
  font-size: 15px;
  font-weight: 600;
  border-radius: 10px;
  background: linear-gradient(135deg, #409eff, #6366f1);
  border: none;
  transition: all 0.3s;
}

.import-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(64, 158, 255, 0.3);
}

.import-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ===== 批量进度 ===== */
.batch-progress {
  margin-top: 16px;
  padding: 14px;
  background: rgba(64, 158, 255, 0.04);
  border: 1px solid rgba(64, 158, 255, 0.1);
  border-radius: 10px;
}

.progress-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #409eff;
  margin-bottom: 12px;
}

.progress-items {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.progress-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.02);
  transition: all 0.2s;
}

.progress-item.status-success {
  background: rgba(103, 194, 58, 0.06);
}

.progress-item.status-error {
  background: rgba(245, 108, 108, 0.06);
}

.progress-item.status-importing {
  background: rgba(64, 158, 255, 0.06);
}

.progress-item-status {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.icon-success { color: #67c23a; }
.icon-error { color: #f56c6c; }
.icon-importing { color: #409eff; animation: spin 1s linear infinite; }
.icon-pending { color: #6b7084; }

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.progress-item-name {
  flex: 1;
  color: #c0c4cc;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress-item-info {
  color: #8b92a5;
  font-size: 11px;
  white-space: nowrap;
}

/* ===== 历史表格 ===== */
.history-card :deep(.el-table) {
  --el-table-text-color: #409eff;
  --el-table-header-text-color: #e4e7ed;
}

.history-card :deep(.el-table td.el-table__cell) {
  color: #409eff;
}

.count-value {
  font-weight: 600;
}

/* ===== 动画 ===== */
.slide-fade-enter-active {
  transition: all 0.3s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s ease-in;
}

.slide-fade-enter-from {
  opacity: 0;
  transform: translateY(-8px);
}

.slide-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* ===== Element Plus 覆盖 ===== */
:deep(.el-card__header) {
  padding: 14px 20px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

:deep(.el-card__body) {
  padding: 20px;
}

:deep(.el-input__wrapper),
:deep(.el-select .el-input .el-input__wrapper) {
  background-color: rgba(18, 22, 32, 0.6);
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.1) inset;
}

:deep(.el-input__inner) {
  color: #e4e7ed;
}

:deep(.el-input__inner::placeholder) {
  color: #6b7084;
}

:deep(.el-table) {
  --el-table-bg-color: transparent;
  --el-table-tr-bg-color: transparent;
  --el-table-header-bg-color: rgba(25, 30, 42, 0.8);
  --el-table-row-hover-bg-color: rgba(64, 158, 255, 0.06);
  --el-table-border-color: rgba(255, 255, 255, 0.06);
  --el-table-text-color: #c0c4cc;
  --el-table-header-text-color: #e4e7ed;
}

:deep(.el-table th.el-table__cell) {
  background-color: rgba(25, 30, 42, 0.9) !important;
  color: #e4e7ed;
  font-size: 12px;
  font-weight: 600;
}

:deep(.el-table td.el-table__cell) {
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  font-size: 12px;
  padding: 6px 0;
}

:deep(.el-table--striped .el-table__body tr.el-table__row--striped td.el-table__cell) {
  background: rgba(255, 255, 255, 0.02);
}

:deep(.el-table .el-table__row:hover > td) {
  background: rgba(64, 158, 255, 0.05) !important;
}

:deep(.el-empty__description p) {
  color: #6b7084;
}

:deep(.el-checkbox__label) {
  color: #a0a5b8;
  font-size: 13px;
}

:deep(.el-progress__text) {
  color: #a0a5b8;
  font-size: 12px !important;
}

:deep(.el-progress-bar__outer) {
  background-color: rgba(255, 255, 255, 0.06) !important;
}

/* ===== 滚动条 ===== */
.files-list::-webkit-scrollbar {
  width: 4px;
}

.files-list::-webkit-scrollbar-track {
  background: transparent;
}

.files-list::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
}

/* ===== 响应式 ===== */
@media (max-width: 1024px) {
  .import-layout {
    flex-direction: column;
  }
  .history-section {
    width: 100%;
  }
}

@media (max-width: 767px) {
  .import-page { padding: 16px; }
  .page-title { font-size: 16px; }
  .form-row {
    flex-direction: column;
    gap: 12px;
  }
  .option-wrap {
    padding-top: 0;
  }
}
</style>
