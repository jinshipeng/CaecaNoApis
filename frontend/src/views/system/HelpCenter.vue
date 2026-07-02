<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  HelpFilled,
  CaretBottom,
  Document,
  Download,
  Phone,
  Message,
  ChatSquare,
  Warning,
  InfoFilled,
  SetUp,
  DataAnalysis,
  MagicStick
} from '@element-plus/icons-vue'

const activeTab = ref('faq')

interface FAQItem {
  id: number
  question: string
  answer: string
  expanded: boolean
}

const faqs = ref<FAQItem[]>([
  {
    id: 1,
    question: '如何创建销售订单？',
    answer: '进入「供应链管理 > 销售订单」页面，点击「新增订单」按钮，选择客户、物料，填写数量和交期等信息。系统会自动计算总金额并检查物料库存情况。',
    expanded: false
  },
  {
    id: 2,
    question: '采购订单是如何自动生成的？',
    answer: '当销售订单创建或变更时，系统会根据 BOM 结构和当前库存自动进行 MRP 计算，生成采购建议。在「物料计划」页面可以查看短缺分析和采购建议列表，确认后一键生成采购订单。',
    expanded: false
  },
  {
    id: 3,
    question: '库存预警如何工作？',
    answer: '进入「供应链管理 > 库存管理」页面，每个物料的库存卡片会显示安全库存状态。当实际库存低于安全库存的 50% 时显示红色预警，低于安全库存时显示黄色警告。系统会自动计算动态安全库存值。',
    expanded: false
  },
  {
    id: 4,
    question: '如何批量导入数据？',
    answer: '进入「系统管理 > 数据导入」页面，支持 Excel 格式的批量导入。可导入的数据类型包括：物料主数据、客户信息、供应商信息、BOM 结构等。请先下载对应模板，按格式填写后上传。',
    expanded: false
  },
  {
    id: 5,
    question: 'AI 智能分析功能怎么用？',
    answer: '进入「计划与产能 > AI 智能分析」页面，可选择需求预测（基于历史数据的未来销量预测）和情景模拟（模拟不同参数对供应链的影响）。选择预测天数和物料后点击执行即可。',
    expanded: false
  },
  {
    id: 6,
    question: '可视化大屏展示什么内容？',
    answer: '进入「可视化大屏」页面，实时展示：库存总量与分布、齐套率趋势、产能利用率、订单交付风险等关键指标。适合在车间大屏幕上做生产监控展示。',
    expanded: false
  },
  {
    id: 7,
    question: '数字孪生功能是什么？',
    answer: '数字孪生页面提供产线的虚拟仿真视图，实时映射物理产线的运行状态、设备状态和生产进度。可用于远程监控和异常预警。',
    expanded: false
  },
  {
    id: 8,
    question: '如何查看操作审计日志？',
    answer: '进入「系统管理 > 审计日志」页面，可以查看所有用户的操作记录，包括登录记录、数据修改记录、导入导出记录等，支持按时间范围和操作类型筛选。',
    expanded: false
  }
])

interface DocItem {
  id: number
  title: string
  category: string
  size: string
  date: string
  description: string
  filename: string
}

const documentList = ref<DocItem[]>([
  {
    id: 1,
    title: '联宝智能供应链系统操作手册',
    category: '操作指南',
    size: '2.3MB',
    date: '2026-06-02',
    description: '包含所有功能模块的详细操作步骤说明',
    filename: 'user-guide.md'
  },
  {
    id: 2,
    title: 'API 接口文档',
    category: '开发文档',
    size: '1.1MB',
    date: '2026-06-02',
    description: 'RESTful API 完整接口定义及调用示例',
    filename: 'api-docs.md'
  },
  {
    id: 3,
    title: '数据导入模板包',
    category: '操作指南',
    size: '512KB',
    date: '2026-06-02',
    description: '物料/客户/供应商/BOM 等Excel导入模板',
    filename: 'import-templates.zip'
  },
  {
    id: 4,
    title: '系统部署指南',
    category: '运维文档',
    size: '896KB',
    date: '2026-06-02',
    description: 'Docker 部署、数据库配置、环境变量说明',
    filename: 'deployment-guide.md'
  },
  {
    id: 5,
    title: '常见问题解答 (FAQ)',
    category: '操作指南',
    size: '256KB',
    date: '2026-06-02',
    description: '系统使用中常见问题及解决方案汇总',
    filename: 'faq.md'
  }
])

const toggleFaq = (index: number) => {
  faqs.value[index].expanded = !faqs.value[index].expanded
}

const handleDownload = (item: DocItem) => {
  const content = generateDocContent(item)
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = item.filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
  ElMessage.success(`已开始下载：${item.title}`)
}

function generateDocContent(item: DocItem): string {
  const docs: Record<string, string> = {
    'user-guide.md': generateUserGuide(),
    'api-docs.md': generateApiDocs(),
    'import-templates.md': generateImportTemplates(),
    'deployment-guide.md': generateDeploymentGuide(),
    'faq.md': generateFaqDoc()
  }
  return docs[item.filename] || '# 文档生成中...\n\n该文档正在准备中，请联系管理员获取最新版本。'
}

function generateUserGuide(): string {
  return `# 联宝智能供应链系统 - 操作手册

> 版本: 2.0 | 更新日期: ${new Date().toLocaleDateString('zh-CN')}

---

## 目录

1. [系统概述](#系统概述)
2. [快速开始](#快速开始)
3. [功能模块详解](#功能模块详解)
4. [常见操作流程](#常见操作流程)

---

## 系统概述

联宝智能供应链管理系统是一套面向制造业的智能化供应链协同平台，涵盖以下核心功能：

| 模块 | 功能描述 |
|------|----------|
| **仪表盘** | 业务数据概览、KPI指标监控、趋势图表 |
| **物料管理** | 物料主数据维护、分类管理 |
| **BOM管理** | 产品结构清单维护、版本控制 |
| **客户管理** | 客户信息维护、信用评级 |
| **供应商管理** | 供应商资质管理、绩效评估 |
| **销售订单** | 订单录入、跟踪、发货管理 |
| **物料计划** | MRP运算、短缺分析、采购建议 |
| **库存管理** | 库存查询、预警、盘点 |
| **采购订单** | 采购申请、审批、到货管理 |
| **产能管理** | 产线能力规划、负荷分析 |
| **智能分析** | 需求预测、情景模拟 |
| **可视化大屏** | 生产监控大屏展示 |
| **数字孪生** | 产线虚拟仿真 |

---

## 快速开始

### 1. 登录系统

1. 打开浏览器访问系统地址
2. 输入账号密码（默认: admin / admin123）
3. 点击「登 录」按钮

### 2. 导航说明

左侧导航栏按功能分组：
- **仪表盘** — 系统首页，展示核心指标
- **数据管理** — 基础数据维护（物料、客户、供应商、BOM）
- **供应链管理** — 核心业务（销售订单、库存、采购）
- **计划与产能** — 规划类功能（物料计划、产能）
- **系统** — 系统管理功能（数据导入、审计日志、帮助中心）

---

## 功能模块详解

### 销售订单

**路径**: 供应链管理 → 销售订单

**操作流程**:
1. 点击「新增订单」
2. 选择客户（从下拉列表选择已有客户）
3. 选择物料（从下拉列表选择）
4. 填写数量、单价、交货日期
5. 选择优先级（1=紧急 / 3=普通 / 5=低优）
6. 点击保存

**字段说明**:
- 订单号: 系统自动生成，唯一标识
- 状态: pending(待处理) → confirmed(已确认) → shipped(已发货) → completed(已完成)
- 优先级: 数字越小越紧急

### 物料计划

**路径**: 计划与产能 → 物料计划

**核心功能**:
- **MRP 运算**: 根据销售订单和BOM自动计算物料需求
- **短缺分析**: 显示库存低于安全库存的物料清单
- **采购建议**: 自动生成建议采购量和时间
- **根因分析**: 分析缺料的根本原因（供应商延迟？需求突变？）

### AI 智能分析

**路径**: 计划与产能 → AI 智能分析

**功能一: 需求预测**
- 选择预测物料和时间范围
- 系统基于历史订单数据进行机器学习预测
- 输出未来N天的预计需求量

**功能二: 情景模拟**
- 选择模拟场景（如：供应商延迟、需求翻倍）
- 设置影响参数
- 系统模拟对整体供应链的影响

---

## 常见操作流程

### 流程一: 从接单到采购

\`\`\`
销售订单创建
    ↓
MRP 自动运算
    ↓
物料短缺分析
    ↓
生成采购建议
    ↓
确认并创建采购订单
    ↓
采购到货入库
\`\`\`

### 流程二: 数据批量导入

\`\`\`
下载对应导入模板
    ↓
按模板格式填写数据
    ↓
进入 数据导入 页面
    ↓
选择文件上传
    ↓
查看导入结果报告
\`\`\`

---

*更多详细信息请联系技术支持团队*
`
}

function generateApiDocs(): string {
  return `# 联宝智能供应链系统 - API 接口文档

> Base URL: \`http://localhost:8000/api/\`
> 认证方式: Token (Header: Authorization: Token <token>)

---

## 认证接口

### POST /auth/login/
登录获取Token

**请求体**:
\`\`\`json
{
  "username": "admin",
  "password": "admin123"
}
\`\`\`

**响应**:
\`\`\`json
{
  "token": "784d44b67b802c9ade07adf43880ad97de8e270d",
  "user_id": 1,
  "username": "admin",
  "is_staff": true
}
\`\`\`

### POST /auth/logout/
退出登录

### GET /auth/user/
获取当前用户信息

---

## 物料接口

### GET /materials/
获取物料列表

**Query 参数**:
| 参数 | 类型 | 说明 |
|------|------|------|
| page | int | 页码，默认1 |
| page_size | int | 每页条数，默认10 |
| keyword | str | 搜索关键词 |

**响应**:
\`\`\`json
{
  "count": 100,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "material_code": "MAT-001",
      "material_name": "主板A型",
      "unit": "PCS",
      "category": "电子元件"
    }
  ]
}
\`\`\`

### POST /materials/
创建物料

### PUT /materials/{id}/
更新物料

### DELETE /materials/{id}/
删除物料

---

## 销售订单接口

### GET /orders/
获取订单列表

### POST /orders/
创建订单

### PUT /orders/{id}/
更新订单

### DELETE /orders/{id}/
删除订单

### GET /orders/planning_summary/
获取计划摘要

### GET /orders/shortage_report/
获取短缺报告

---

## 采购订单接口

### GET /purchase-orders/
获取采购订单列表

### POST /purchase-orders/
创建采购订单

---

## Dashboard 接口

### GET /dashboard/stats/
获取仪表盘统计数据

### GET /screen/data/
获取可视化大屏数据

---

*完整接口文档持续更新中...*
`
}

function generateImportTemplates(): string {
  return `# 数据导入模板说明

---

## 支持的导入类型

### 1. 物料主数据 (Material)

**必填字段**:
| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| material_code | string | 物料代码(唯一) | MAT-001 |
| material_name | string | 物料名称 | 主板A型 |
| unit | string | 单位 | PCS |
| category | string | 分类 | 电子元件 |
| standard_cost | decimal | 标准成本 | 150.00 |
| safety_stock | int | 安全库存 | 100 |
| lead_time | int | 供货周期(天) | 7 |

**示例行**: \`MAT-001,主板A型,PCS,电子元件,150.00,100,7\`

### 2. 客户信息 (Customer)

**必填字段**:
| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| customer_code | string | 客户代码(唯一) | CUS-001 |
| customer_name | string | 客户名称 | 联想集团 |
| contact_person | string | 联系人 | 张三 |
| phone | string | 电话 | 13800138000 |
| address | string | 地址 | 北京市海淀区 |

### 3. 供应商信息 (Supplier)

**必填字段**:
| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| supplier_code | string | 供应商代码(唯一) | SUP-001 |
| supplier_name | string | 供应商名称 | 华为公司 |
| contact_person | string | 联系人 | 李四 |
| phone | string | 电话 | 13900139000 |
| rating | int | 评级(1-5) | 4 |

### 4. BOM 结构 (BOM)

**必填字段**:
| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| product_code | string | 产品代码 | PRD-001 |
| product_name | string | 产品名称 | 笔记本电脑A |
| material_code | string | 子件物料代码 | MAT-001 |
| quantity | decimal | 用量 | 1.0 |
| level | int | 层级 | 1 |

---

## 导入注意事项

1. 使用 UTF-8 编码的 CSV 或 Excel 文件
2. 第一行为表头，与上述字段名一致
3. 日期格式: YYYY-MM-DD
4. 数值类型不要带千分位逗号
5. 单次导入不超过 1000 行

---
`
}

function generateDeploymentGuide(): string {
  return `# 联宝智能供应链系统 - 部署指南

---

## 环境要求

| 组件 | 版本要求 |
|------|----------|
| Python | 3.10+ |
| Node.js | 18+ |
| PostgreSQL | 14+ (或 SQLite 开发环境) |
| Redis | 6+ (可选，用于缓存) |

---

## 快速部署

### 方式一: 一键启动脚本

双击运行项目根目录的 \`一键安装并启动-新版.bat\` 即可。

### 方式二: 手动部署

#### 1. 后端启动

\`\`\`bash
cd system
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
\`\`\`

#### 2. 前端启动

\`\`\`bash
cd frontend
npm install
npm run dev
\`\`\`

#### 3. 访问系统

- 前端地址: http://localhost:3001
- 后端API: http://localhost:8000/api/

---

## 生产环境配置

### 环境变量

在 \`system/.env\` 中配置:

\`\`\`
DEBUG=False
SECRET_KEY=your-secret-key-here
DATABASE_URL=postgresql://user:pass@localhost:5432/supply_chain
REDIS_URL=redis://localhost:6379/0
ALLOWED_HOSTS=your-domain.com
CORS_ORIGINS=https://your-frontend-domain.com
\`\`\`

### Nginx 配置示例

\`\`\`nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:3001;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
    }
}
\`\`\`

---
`
}

function generateFaqDoc(): string {
  return `# 联宝智能供应链系统 - 常见问题解答 (FAQ)

---

## 一、登录与权限

### Q1: 提示"Token无效"怎么办？
**A:** 清除浏览器缓存后重新登录，或等待Token过期后重新认证。

### Q2: 如何修改密码？
**A:** 在个人设置中修改，或联系管理员协助。

---

## 二、数据操作

### Q3: 删除的数据能恢复吗？
**A:** 目前不支持软删除，删除操作不可逆。请谨慎操作。

### Q4: 批量导入失败怎么办？
**A:** 查看"数据导入"页面的历史记录，下载错误报告修正后重新上传。

### Q5: 为什么有些字段不能编辑？
**A:** 部分关键字段（如物料代码）在创建后锁定，防止关联数据不一致。

---

## 三、功能疑问

### Q6: MRP运算多久更新一次？
**A:** 手动触发。在"物料计划"页面点击"运行MRP"即可重新计算。

### Q7: AI预测准确吗？
**A:** 预测结果仅供参考，建议结合人工判断决策。数据量越大，预测越准确。

### Q8: 库存预警阈值可以自定义吗？
**A:** 可以。在物料主数据中编辑各物料的"安全库存"字段即可。

---

## 四、性能与故障

### Q9: 页面加载慢怎么办？
**A:** 
1. 检查网络连接
2. 清除浏览器缓存
3. 联系IT确认服务器状态

### Q10: 导出数据量太大怎么办？
**A:** 使用筛选条件缩小范围后再导出，或联系管理员后台处理。

---

*如有其他问题，请联系技术支持: support@lenovo.com*
`
}
</script>

<template>
  <div class="help-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">帮助中心</h1>
        <p class="page-desc">联宝智能供应链系统使用帮助和文档中心</p>
      </div>
    </div>

    <el-tabs v-model="activeTab" type="border-card" class="help-tabs">
      <el-tab-pane name="faq">
        <template #label>
          <span class="tab-label"><el-icon><HelpFilled /></el-icon> 常见问题</span>
        </template>
        <div class="faq-list">
          <div
            v-for="(faq, index) in faqs"
            :key="faq.id"
            class="faq-item"
          >
            <div class="faq-header" @click="toggleFaq(index)">
              <span class="faq-question">
                <el-icon><HelpFilled /></el-icon>
                {{ faq.question }}
              </span>
              <el-icon :class="{ rotated: faq.expanded }"><CaretBottom /></el-icon>
            </div>
            <transition name="expand">
              <div class="faq-content" v-show="faq.expanded">
                {{ faq.answer }}
              </div>
            </transition>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane name="document">
        <template #label>
          <span class="tab-label"><el-icon><Document /></el-icon> 文档下载</span>
        </template>
        <div class="document-list">
          <div
            v-for="item in documentList"
            :key="item.id"
            class="document-item"
          >
            <div class="document-info">
              <el-icon class="doc-icon"><Document /></el-icon>
              <div>
                <h4>{{ item.title }}</h4>
                <p class="doc-desc">{{ item.description }}</p>
                <div class="doc-meta">
                  <el-tag size="small" :type="getCategoryType(item.category)">{{ item.category }}</el-tag>
                  <span class="doc-size">{{ item.size }}</span>
                  <span class="doc-date">{{ item.date }}</span>
                </div>
              </div>
            </div>
            <el-button type="primary" size="small" @click="handleDownload(item)">
              <el-icon><Download /></el-icon>
              下载
            </el-button>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane name="contact">
        <template #label>
          <span class="tab-label"><el-icon><Message /></el-icon> 联系我们</span>
        </template>
        <div class="contact-info">
          <div class="contact-card">
            <el-icon class="contact-icon"><Phone /></el-icon>
            <div>
              <h4>技术支持热线</h4>
              <p class="contact-value">400-960-6666</p>
              <p class="contact-hint">工作时间：周一至周五 9:00-18:00</p>
            </div>
          </div>

          <div class="contact-card">
            <el-icon class="contact-icon"><Message /></el-icon>
            <div>
              <h4>邮箱地址</h4>
              <p class="contact-value">scm-support@lenovo.com</p>
              <p class="contact-hint">工作日24小时内回复</p>
            </div>
          </div>

          <div class="contact-card">
            <el-icon class="contact-icon"><ChatSquare /></el-icon>
            <div>
              <h4>内部工单系统</h4>
              <p class="contact-value">OA 系统 → IT服务台</p>
              <p class="contact-hint">紧急问题请走工单通道，响应更快</p>
            </div>
          </div>

          <div class="quick-links">
            <h4 class="quick-title">快捷链接</h4>
            <div class="link-grid">
              <a href="/dashboard" class="quick-link">
                <el-icon><InfoFilled /></el-icon>
                <span>返回首页</span>
              </a>
              <a href="/audit" class="quick-link">
                <el-icon><SetUp /></el-icon>
                <span>查看审计日志</span>
              </a>
              <a href="/ai-analysis" class="quick-link">
                <el-icon><MagicStick /></el-icon>
                <span>AI 智能分析</span>
              </a>
              <a href="/screen" class="quick-link">
                <el-icon><DataAnalysis /></el-icon>
                <span>可视化大屏</span>
              </a>
            </div>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <div class="footer-info">
      <p>
        <el-icon><Warning /></el-icon>
        如遇系统异常，请优先截图保留错误信息后联系技术支持
      </p>
    </div>
  </div>
</template>

<script lang="ts">
function getCategoryType(category: string): 'success' | 'warning' | undefined {
  const map: Record<string, 'success' | 'warning'> = {
    '开发文档': 'success',
    '运维文档': 'warning'
  }
  return map[category] || undefined
}
</script>

<style scoped lang="scss">
.help-page {
  max-width: 900px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 24px;

  .page-title {
    font-size: 28px;
    font-weight: 700;
    color: #E8EAED;
    margin: 0 0 8px 0;
  }

  .page-desc {
    font-size: 14px;
    color: #B0B8C4;
    margin: 0;
  }
}

.help-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 0 !important;
    background: rgba(255, 255, 255, 0.03) !important;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06) !important;

    .el-tabs__nav-wrap::after {
      background: transparent !important;
    }

    .el-tabs__item {
      color: #B0B8C4 !important;

      &:hover {
        color: #E8EAED !important;
      }

      &.is-active {
        color: #6E9EF7 !important;
      }

      .tab-label {
        display: flex;
        align-items: center;
        gap: 6px;
      }
    }
  }

  :deep(.el-tabs__content) {
    padding: 20px;
    background: rgb(31, 35, 48) !important;
  }
}

.faq-list {
  border-radius: 8px;
  overflow: hidden;
}

.faq-item {
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);

  &:last-child {
    border-bottom: none;
  }

  .faq-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 18px 20px;
    cursor: pointer;
    transition: background 0.3s;

    &:hover {
      background: rgba(110, 158, 247, 0.05);
    }

    .faq-question {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 15px;
      color: #E8EAED;
      font-weight: 500;

      :deep(.el-icon) {
        color: #6E9EF7;
        flex-shrink: 0;
      }
    }

    :deep(.el-icon) {
      color: #B0B8C4;
      transition: transform 0.3s ease;

      &.rotated {
        transform: rotate(180deg);
      }
    }
  }

  .faq-content {
    padding: 0 20px 18px 52px;
    font-size: 14px;
    color: #B0B8C4;
    line-height: 1.8;
  }
}

.expand-enter-active,
.expand-leave-active {
  transition: all 0.25s ease;
  overflow: hidden;
}

.expand-enter-from,
.expand-leave-to {
  opacity: 0;
  max-height: 0;
  padding-top: 0;
  padding-bottom: 0;
}

.document-list {
  border-radius: 8px;
  padding: 12px;
}

.document-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  border-radius: 8px;
  transition: background 0.3s;
  background: rgba(255, 255, 255, 0.02);

  &:hover {
    background: rgba(255, 255, 255, 0.05);
  }

  .document-info {
    display: flex;
    align-items: center;
    gap: 16px;

    .doc-icon {
      font-size: 28px;
      color: #6E9EF7;
      flex-shrink: 0;
    }

    h4 {
      font-size: 15px;
      color: #E8EAED;
      margin: 0 0 6px 0;
    }

    .doc-desc {
      font-size: 13px;
      color: #78849E;
      margin: 0 0 8px 0;
    }

    .doc-meta {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 13px;
      color: #78849E;

      .doc-date {
        color: #555F73;
      }
    }
  }
}

.contact-info {
  border-radius: 8px;
  padding: 24px;
}

.contact-card {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 20px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 8px;
  margin-bottom: 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  transition: border-color 0.3s;

  &:hover {
    border-color: rgba(110, 158, 247, 0.3);
  }

  &:last-child {
    margin-bottom: 0;
  }

  .contact-icon {
    font-size: 28px;
    color: #6E9EF7;
    flex-shrink: 0;
  }

  h4 {
    font-size: 15px;
    color: #E8EAED;
    margin: 0 0 8px 0;
  }

  p {
    font-size: 14px;
    color: #B0B8C4;
    margin: 0 0 4px 0;

    &.contact-value {
      color: #6E9EF7;
      font-weight: 600;
      font-size: 15px;
    }

    &.contact-hint {
      font-size: 12px;
      color: #78849E;
    }
  }
}

.quick-links {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);

  .quick-title {
    font-size: 15px;
    color: #E8EAED;
    margin: 0 0 16px 0;
  }

  .link-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
  }

  .quick-link {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 16px;
    background: rgba(255, 255, 255, 0.03);
    border-radius: 8px;
    color: #B0B8C4;
    text-decoration: none;
    font-size: 13px;
    transition: all 0.3s;

    &:hover {
      background: rgba(110, 158, 247, 0.1);
      color: #6E9EF7;
    }

    .el-icon {
      font-size: 16px;
    }
  }
}

.footer-info {
  margin-top: 24px;
  padding: 16px 20px;
  background: rgba(245, 108, 108, 0.08);
  border: 1px solid rgba(245, 108, 108, 0.15);
  border-radius: 8px;

  p {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    font-size: 13px;
    color: #E8A09A;
  }
}

@media (max-width: 767px) {
  .page-title {
    font-size: 22px;
  }

  .link-grid {
    grid-template-columns: repeat(2, 1fr) !important;
  }

  .document-item {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
}
</style>
