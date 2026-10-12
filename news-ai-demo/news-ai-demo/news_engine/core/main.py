# -*- coding: utf-8 -*-
"""
新闻AI系统主入口

整合功能：
1. 处理不需要VPN的网站新闻
2. 处理需要VPN的新闻
3. 处理船厂网站新闻
4. 处理微信公众号文章（WeRSS）
5. 生成每日新闻报告
6. 修复 entity/data JSON 格式
7. 重新处理数据库新闻 - 使用RAG+当前prompt对已有新闻重新分类、提取内容并更新数据库

使用方法:
    python main.py
    
    然后按提示选择功能（0-7）
"""

import asyncio
import sys
import os

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def print_banner():
    """打印欢迎界面"""
    print("\n" + "="*70)
    print("🚢  航运新闻AI处理系统  ")
    print("="*70)
    print()


def print_menu():
    """打印主菜单"""
    print("\n" + "-"*70)
    print("请选择功能：")
    print("-"*70)
    print()
    print("  [1] 📥 处理不需要VPN的网站新闻")
    print("      获取网站新闻（无需VPN源）")
    print("      → URL前置去重 → 分类 → 内容提取 → 导入数据库")
    print()
    print("  [2] 🌐 处理需要VPN的新闻")
    print("      获取新闻（需要VPN源） → URL前置去重 → 分类 → 内容提取 → 导入数据库")
    print()
    print("  [3] 🏭 处理船厂网站新闻")
    print("      获取船厂网站新闻 → URL前置去重 → 分类 → 内容提取 → 导入数据库")
    print()
    print("  [4] 📱 处理微信公众号文章（WeRSS）")
    print("      获取微信公众号文章 → URL前置去重 → 分类 → 内容提取 → 导入数据库")
    print()
    print("  [5] 📄 生成每日新闻报告")
    print("      从数据库提取新闻 → 生成Word文档 → 保存到桌面")
    print()
    print("  [6] 🔄 修复 entity/data JSON 格式")
    print("      检测新造船订单/买卖船/租船/船舶建造/维修改装/公司金融/行业新闻 七类新闻")
    print("      格式错误的用 AI 重新生成，正确的跳过")
    print()
    print("  [7] 🔄 重新处理数据库新闻")
    print("      从数据库读取已有新闻 → 使用RAG+当前prompt重新分类")
    print("      → 重新提取核心内容 → 重新生成报告 → 更新数据库")
    print()
    print("  [0] ❌ 退出系统")
    print()
    print("-"*70)


async def run_complete_pipeline(vpn_mode='all', include_wechat_mp=True):
    """运行完整新闻处理流程"""
    mode_text = "不需要VPN" if vpn_mode == 'no_vpn' else "需要VPN" if vpn_mode == 'vpn' else "微信公众号" if vpn_mode == 'wechat_only' else "完整"
    
    print("\n" + "="*70)
    print(f"📥 功能：处理{mode_text}的新闻")
    print("="*70)
    print()
    
    try:
        from complete_news_pipeline import run_complete_pipeline as _run_pipeline, connect_to_database
        from db_config import DB_CONFIG as db_config
        
        print("📝 即将执行以下步骤：")
        print(f"   1. 获取新闻数据（{mode_text}源爬取）")
        print("   2. AI前置去重")
        print("   3. AI重新分类（8个类别）")
        print("   4. 提取核心内容（5个维度）")
        print("   5. 生成新闻报告")
        print("   6. 导入数据库")
        print()
        
        confirm = input("确认开始? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ 已取消")
            return
        
        # 运行流程
        result = await _run_pipeline(
            enable_db_dedup=True,
            db_config=db_config,
            vpn_mode=vpn_mode,
            include_wechat_mp=include_wechat_mp
        )
        
        if result:
            print("\n" + "="*70)
            print(f"✅ 流程完成！共处理 {len(result)} 条新闻")
            print("="*70)
        else:
            print("\n⚠️  没有新闻需要处理")
            
    except Exception as e:
        print(f"\n❌ 处理失败: {e}")
        import traceback
        traceback.print_exc()


def run_daily_news_generation():
    """运行每日新闻生成"""
    print("\n" + "="*70)
    print("📄 功能2：生成每日新闻报告")
    print("="*70)
    print()
    
    try:
        from daily_news_generation import main as daily_main
        
        print("📝 即将执行以下步骤：")
        print("   1. 从数据库获取今日/昨日新闻")
        print("   2. 按分类整理")
        print("   3. 生成Word文档（适合公众号发布）")
        print("   4. 保存到桌面/每日新闻文件夹")
        print()
        
        # 获取日期范围信息
        from daily_news_generation import get_date_range_for_news
        target_dates = get_date_range_for_news()
        
        confirm = input("确认开始? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ 已取消")
            return
        
        # 运行生成
        daily_main()
        
    except Exception as e:
        print(f"\n❌ 生成失败: {e}")
        import traceback
        traceback.print_exc()


def select_reprocess_steps():
    """
    交互式选择重新处理步骤
    
    Returns:
        list: 选择的步骤列表，按固定顺序排列
    """
    print("\n" + "-"*70)
    print("🔄 请选择要重新处理的步骤（可多选，按固定顺序执行）：")
    print("-"*70)
    print()
    print("  [1] 📂 重新分类 (reclassify)")
    print("      使用AI重新对新闻进行分类（8个类别）")
    print()
    print("  [2] 📝 提取核心内容 (core_content)")
    print("      根据分类提取五个维度的核心内容")
    print()
    print("  [3] 📰 生成新闻报告 (news_report)")
    print("      基于核心内容生成简洁的新闻报告")
    print()
    print("  [4] ✅ 全部选择（1+2+3，按顺序执行）")
    print()
    print("  [5] ❌ 取消")
    print()
    print("-"*70)
    print("提示: 可以同时输入多个选项，如输入 '123' 或 '1,2,3' 表示选择全部")
    print("      处理顺序固定为: reclassify -> core_content -> news_report")
    print("-"*70)
    
    choice = input("\n请输入选项 (如: 1, 12, 123, 23 等): ").strip()
    
    if not choice or choice == '5':
        return None  # 取消
    
    # 解析选择
    selected = set()
    
    # 处理各种输入格式：123、1,2,3、1 2 3、1-3
    choice = choice.replace(',', '').replace(' ', '').replace('-', '')
    
    if '4' in choice or '123' in choice or set(choice) >= {'1', '2', '3'}:
        # 选择了全部
        return ["reclassify", "core_content", "news_report"]
    
    if '1' in choice:
        selected.add("reclassify")
    if '2' in choice:
        selected.add("core_content")
    if '3' in choice:
        selected.add("news_report")
    
    if not selected:
        print("⚠️  未选择有效选项，默认选择全部步骤")
        return ["reclassify", "core_content", "news_report"]
    
    # 按固定顺序排序
    order = {"reclassify": 0, "core_content": 1, "news_report": 2}
    result = sorted(list(selected), key=lambda x: order[x])
    
    return result


def select_categories():
    """
    交互式选择要重新处理的新闻分类
    
    Returns:
        list or None: 选择的分类列表；返回 None 表示取消，返回空列表表示全部
    """
    categories = [
        "新造船订单",
        "买卖船",
        "租船",
        "船舶建造",
        "维修改装",
        "公司金融",
        "行业新闻",
    ]
    
    print("\n" + "-"*70)
    print("🏷️ 请选择要重新处理的新闻分类：")
    print("-"*70)
    print()
    print("  [0] 全部分类（不筛选）")
    for i, cat in enumerate(categories, 1):
        print(f"  [{i}] {cat}")
    print("  [9] 取消")
    print()
    print("-"*70)
    print("提示: 可以同时输入多个选项，如 '123' 或 '1,2,3' 表示选择多个分类")
    print("      输入 0 表示不筛选，处理所有分类")
    print("-"*70)
    
    choice = input("\n请输入选项 (如: 0, 1, 12, 123 等): ").strip()
    
    if choice == '9':
        return None  # 取消
    
    if not choice or choice == '0':
        return []  # 全部
    
    # 解析选择
    choice = choice.replace(',', '').replace(' ', '').replace('-', '')
    selected = []
    seen = set()
    
    for ch in choice:
        if ch.isdigit():
            idx = int(ch)
            if 1 <= idx <= len(categories):
                cat = categories[idx - 1]
                if cat not in seen:
                    selected.append(cat)
                    seen.add(cat)
    
    if not selected:
        print("⚠️  未选择有效分类，默认处理全部分类")
        return []
    
    return selected


async def run_reprocess_database_news():
    """运行数据库新闻重新处理流程"""
    print("\n" + "="*70)
    print("🔄 功能7：重新处理数据库新闻")
    print("="*70)
    print()
    
    try:
        from reprocess_database_news import run_with_config
        
        print("📝 请选择处理范围：")
        print()
        print("  [1] 处理最近7天的新闻")
        print("  [2] 处理最近30天的新闻")
        print("  [3] 处理所有新闻")
        print("  [4] 自定义日期范围")
        print("  [5] 取消")
        print()
        
        choice = input("请输入选项 (1-5): ").strip()
        
        config = {}
        
        if choice == '1':
            config = {"days": 7}
        elif choice == '2':
            config = {"days": 30}
        elif choice == '3':
            config = {"process_all": True}
        elif choice == '4':
            start_date = input("开始日期 (YYYY-MM-DD): ").strip()
            end_date = input("结束日期 (YYYY-MM-DD): ").strip()
            config = {
                "start_date": start_date if start_date else None,
                "end_date": end_date if end_date else None,
            }
        else:
            print("❌ 已取消")
            return
        
        # 选择处理步骤
        steps = select_reprocess_steps()
        if steps is None:
            print("❌ 已取消")
            return
        config["reprocess_steps"] = steps
        
        # 选择要处理的分类
        categories = select_categories()
        if categories is None:
            print("❌ 已取消")
            return
        config["categories"] = categories
        
        # 询问限制数量（用于测试）
        limit_input = input("\n是否限制处理数量? (输入数字限制，直接回车不限制): ").strip()
        if limit_input.isdigit():
            config["limit"] = int(limit_input)
            print(f"   限制处理 {limit_input} 条")
        
        print("\n" + "="*70)
        print("📋 处理配置确认：")
        print("="*70)
        if config.get("process_all"):
            print("   日期范围: 全部")
        elif config.get("days"):
            print(f"   日期范围: 最近 {config['days']} 天")
        elif config.get("start_date") or config.get("end_date"):
            print(f"   日期范围: {config.get('start_date', '不限')} 至 {config.get('end_date', '不限')}")
        print(f"   处理步骤: {' -> '.join(steps)}")
        if categories:
            print(f"   新闻分类: {', '.join(categories)}")
        else:
            print("   新闻分类: 全部")
        print("="*70)
        
        print()
        confirm = input("确认开始? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ 已取消")
            return
        
        # 运行处理
        await run_with_config(config)
        
    except Exception as e:
        print(f"\n❌ 处理失败: {e}")
        import traceback
        traceback.print_exc()


async def run_fix_entity_data_format():
    """运行 entity/data JSON 格式修复"""
    print("\n" + "="*70)
    print("🔄 功能6：修复 entity/data JSON 格式")
    print("="*70)
    print()
    
    try:
        from fix_entity_data_format import run_fix_entity_data_format as _run_fix
        
        print("📝 请选择处理范围：")
        print()
        print("  [1] 处理最近7天的新闻")
        print("  [2] 处理最近30天的新闻")
        print("  [3] 处理所有新闻")
        print("  [4] 自定义日期范围")
        print("  [5] 取消")
        print()
        
        choice = input("请输入选项 (1-5): ").strip()
        
        config = {}
        
        if choice == '1':
            config = {"days": 7}
        elif choice == '2':
            config = {"days": 30}
        elif choice == '3':
            config = {}
        elif choice == '4':
            start_date = input("开始日期 (YYYY-MM-DD): ").strip()
            end_date = input("结束日期 (YYYY-MM-DD): ").strip()
            config = {
                "start_date": start_date if start_date else None,
                "end_date": end_date if end_date else None,
            }
        else:
            print("❌ 已取消")
            return
        
        # 询问并发数
        concurrent_input = input("\n并发数 (直接回车默认3): ").strip()
        config["concurrent"] = int(concurrent_input) if concurrent_input.isdigit() else 3
        
        # 询问是否限制数量（用于测试）
        limit_input = input("\n是否限制处理数量? (输入数字限制，直接回车不限制): ").strip()
        if limit_input.isdigit():
            config["limit"] = int(limit_input)
            print(f"   限制处理 {limit_input} 条")
        
        print("\n" + "="*70)
        print("📋 处理配置确认：")
        print("="*70)
        if config.get("days"):
            print(f"   日期范围: 最近 {config['days']} 天")
        elif config.get("start_date") or config.get("end_date"):
            print(f"   日期范围: {config.get('start_date', '不限')} 至 {config.get('end_date', '不限')}")
        else:
            print(f"   日期范围: 全部")
        print(f"   并发数: {config.get('concurrent', 3)}")
        print("="*70)
        
        print()
        confirm = input("确认开始? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ 已取消")
            return
        
        # 运行处理
        await _run_fix(**config)
        
    except Exception as e:
        print(f"\n❌ 处理失败: {e}")
        import traceback
        traceback.print_exc()


def main():
    """主函数"""
    print_banner()
    
    while True:
        print_menu()
        
        try:
            choice = input("请输入选项 (0-7): ").strip()
            
            if choice == '1':
                asyncio.run(run_complete_pipeline(vpn_mode='no_vpn', include_wechat_mp=False))
            elif choice == '2':
                asyncio.run(run_complete_pipeline(vpn_mode='vpn', include_wechat_mp=False))
            elif choice == '3':
                asyncio.run(run_complete_pipeline(vpn_mode='shipyard', include_wechat_mp=False))
            elif choice == '4':
                asyncio.run(run_complete_pipeline(vpn_mode='wechat_only', include_wechat_mp=True))
            elif choice == '5':
                run_daily_news_generation()
            elif choice == '6':
                asyncio.run(run_fix_entity_data_format())
            elif choice == '7':
                asyncio.run(run_reprocess_database_news())
            elif choice == '0':
                print("\n" + "="*70)
                print("👋 感谢使用，再见！")
                print("="*70)
                break
            else:
                print("\n⚠️  无效选项，请重新输入")
                
        except KeyboardInterrupt:
            print("\n\n" + "="*70)
            print("👋 程序被用户中断")
            print("="*70)
            break
        except Exception as e:
            print(f"\n❌ 发生错误: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
