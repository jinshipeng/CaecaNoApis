from django.shortcuts import render
from django.http import JsonResponse


HELP_ARTICLES = [
    {
        'id': 'material',
        'title': '物料管理',
        'icon': 'fas fa-box',
        'color': '#60a5fa',
        'desc': '学习如何创建、编辑和管理物料主数据，包括物料分类、安全库存设置和BOM关联。',
        'link': '/master/master/m/aterial/',
    },
    {
        'id': 'order',
        'title': '订单管理',
        'icon': 'fas fa-shopping-cart',
        'color': '#10b981',
        'desc': '了解销售订单和采购订单的完整流程，从创建到跟踪交付状态。',
        'link': '/sales-order',
    },
    {
        'id': 'inventory',
        'title': '库存管理',
        'icon': 'fas fa-warehouse',
        'color': '#f59e0b',
        'desc': '掌握库存入库、出库操作，库存预警设置和库存周转分析。',
        'link': '/inventory',
    },
    {
        'id': 'report',
        'title': '报表分析',
        'icon': 'fas fa-chart-pie',
        'color': '#8b5cf6',
        'desc': '使用报表中心进行订单履约分析、库存周转率计算和供应商绩效评估。',
        'link': '/reports/',
    },
]

FAQ_LIST = [
    {
        'q': '如何创建新的物料？',
        'a': '进入"基础数据 > 物料管理"页面，点击"新增物料"按钮，填写物料编码、名称、类型、单位等信息后保存即可。物料编码需唯一，不可重复。',
    },
    {
        'q': '如何运行物料需求计划？',
        'a': '进入"计划管理 > 物料计划"页面，点击"运行物料计划"按钮。系统将根据当前销售订单、BOM结构和库存数据自动计算物料需求，并生成缺料报告。',
    },
    {
        'q': '库存预警是如何触发的？',
        'a': '当物料的当前库存低于设定的安全库存时，系统会自动触发库存预警。预警信息会显示在仪表盘和消息中心。您可以在物料管理中设置每个物料的安全库存值。',
    },
    {
        'q': '如何进行一键排产？',
        'a': '进入"计划管理 > 排产计划"页面，系统会根据销售订单优先级、物料齐套情况和产能约束自动生成排产方案。您可以手动调整后确认排产。',
    },
    {
        'q': '如何导入批量数据？',
        'a': '进入"系统管理 > 数据导入"页面，下载对应的Excel模板，按照模板格式填写数据后上传。系统支持物料、供应商、客户、BOM等数据的批量导入。',
    },
    {
        'q': '如何查看供应商绩效？',
        'a': '进入"报表中心 > 供应商绩效"页面，可以查看供应商的交付可靠率、准时率、质量合格率等指标。支持按时间段筛选和导出报表。',
    },
    {
        'q': '系统支持哪些数据导出格式？',
        'a': '系统支持Excel（.xlsx）格式的数据导出。在各个列表页面，点击"导出"按钮即可将当前数据导出为Excel文件。物料计划结果也支持导出。',
    },
    {
        'q': '如何设置用户权限？',
        'a': '目前系统采用统一的用户认证机制。管理员可以通过Django后台管理界面（/admin/）进行用户管理和权限配置。',
    },
    {
        'q': '数据备份如何操作？',
        'a': '进入"系统管理 > 数据备份"页面，点击"创建备份"按钮即可生成数据库备份文件。备份文件支持下载保存和一键恢复操作。',
    },
    {
        'q': '如何查看操作审计日志？',
        'a': '进入"系统管理 > 审计日志"页面，可以查看所有用户的操作记录，包括操作时间、操作类型、操作对象和操作详情。支持按时间范围和操作类型筛选。',
    },
]

KEYBOARD_SHORTCUTS = [
    {'keys': 'Alt + H', 'desc': '打开帮助中心'},
    {'keys': 'Alt + D', 'desc': '返回仪表盘'},
    {'keys': 'Alt + N', 'desc': '打开通知面板'},
    {'keys': 'Alt + M', 'desc': '切换侧边栏'},
    {'keys': 'Alt + S', 'desc': '聚焦搜索框'},
    {'keys': 'Esc', 'desc': '关闭弹窗/面板'},
]


def help_center(request):
    return render(request, 'help_center.html', {
        'page_title': '帮助中心',
        'page_subtitle': '快速了解系统功能，获取使用指导',
        'articles': HELP_ARTICLES,
        'faq_list': FAQ_LIST,
        'shortcuts': KEYBOARD_SHORTCUTS,
    })


def help_search(request):
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({'results': []})

    results = []
    for article in HELP_ARTICLES:
        if query.lower() in article['title'].lower() or query.lower() in article['desc'].lower():
            results.append({
                'title': article['title'],
                'desc': article['desc'],
                'link': article['link'],
                'icon': article['icon'],
            })

    for faq in FAQ_LIST:
        if query.lower() in faq['q'].lower() or query.lower() in faq['a'].lower():
            results.append({
                'title': faq['q'],
                'desc': faq['a'],
                'link': '',
                'icon': 'fas fa-question-circle',
            })

    return JsonResponse({'results': results})
