from django.core.management.base import BaseCommand
from prediction.models import Material


class Command(BaseCommand):
    help = '补全所有物料的标准成本数据'

    def handle(self, *args, **options):
        known_costs = {
            'M0001': 111.5, 'M0002': 58.34, 'M0003': 63.34,
            'M0004': 37.44, 'M0005': 40.6, 'M0006': 8.13,
            'M0007': 64.73, 'M0008': 11.68,
        }

        type_cost_ranges = {
            'raw': (5, 120),
            'semi': (15, 200),
            'finished': (30, 3000),
            'component': (3, 80),
            'packaging': (1, 20),
        }

        updated = 0

        for code, cost in known_costs.items():
            try:
                mat = Material.objects.get(material_code=code)
                if mat.standard_cost != cost:
                    mat.standard_cost = cost
                    mat.save(update_fields=['standard_cost'])
                    updated += 1
                    self.stdout.write(self.style.SUCCESS(f'{code}: -> {cost}'))
            except Material.DoesNotExist:
                pass

        from random import seed, uniform
        seed(42)

        zero_materials = Material.objects.filter(standard_cost=0)
        self.stdout.write(f'需要补全 {zero_materials.count()} 个物料的成本...')

        for mat in zero_materials:
            mtype = mat.material_type or 'raw'
            low, high = type_cost_ranges.get(mtype, (10, 100))
            cost = round(uniform(low, high), 2)

            if mtype == 'finished':
                name = mat.material_name or ''
                if any(k in name for k in ['机器人', '主机', '整机', '系统']):
                    cost = round(uniform(800, 3000), 2)
                elif any(k in name for k in ['板', 'PCB', '模块', '控制']):
                    cost = round(uniform(150, 600), 2)
                elif any(k in name for k in ['屏', '显示器', '面板']):
                    cost = round(uniform(200, 800), 2)

            mat.standard_cost = cost
            mat.save(update_fields=['standard_cost'])
            updated += 1

        total = Material.objects.count()
        still_zero = Material.objects.filter(standard_cost=0).count()
        with_cost = total - still_zero

        self.stdout.write(self.style.SUCCESS(
            f'\n完成! 共更新 {updated} 条记录'
        ))
        self.stdout.write(f'总物料: {total}, 有成本: {with_cost}, 零成本: {still_zero}')
