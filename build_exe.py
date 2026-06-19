#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键打包脚本
自动清理旧打包数据，在虚拟环境中打包 complete_exporter.py 为 .exe
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# Windows编码兼容性处理
if sys.platform == 'win32':
    import io
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def print_step(message):
    """打印步骤信息"""
    print("\n" + "=" * 60)
    print(f"  {message}")
    print("=" * 60)


def clean_old_build():
    """清理旧的打包数据"""
    print_step("步骤 1/4: 清理旧的打包数据")

    dirs_to_clean = ['build', 'dist', '__pycache__']
    files_to_clean = ['*.spec']

    # 删除目录
    for dir_name in dirs_to_clean:
        if os.path.exists(dir_name):
            print(f"  删除目录: {dir_name}")
            shutil.rmtree(dir_name)
        else:
            print(f"  目录不存在: {dir_name}")

    # 删除.spec文件
    for spec_file in Path('.').glob('*.spec'):
        print(f"  删除文件: {spec_file}")
        spec_file.unlink()

    print("  ✓ 旧数据清理完成")


def check_dependencies():
    """检查并安装依赖"""
    print_step("步骤 2/5: 检查依赖")

    # 检查pysrt
    try:
        import pysrt
        print("  ✓ pysrt已安装")
    except ImportError:
        print("  ⚠ pysrt未安装")
        print("  正在安装pysrt...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pysrt"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("  ✓ pysrt安装成功")
        else:
            print("  ✗ pysrt安装失败")
            print(f"  错误: {result.stderr}")
            return False

    # 检查psutil
    try:
        import psutil
        print("  ✓ psutil已安装")
    except ImportError:
        print("  ⚠ psutil未安装")
        print("  正在安装psutil...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "psutil"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("  ✓ psutil安装成功")
        else:
            print("  ✗ psutil安装失败")
            print(f"  错误: {result.stderr}")
            return False

    # 检查PyInstaller
    try:
        import PyInstaller
        print(f"  ✓ PyInstaller已安装 (版本: {PyInstaller.__version__})")
        return True
    except ImportError:
        print("  ⚠ PyInstaller未安装")
        print("  正在安装PyInstaller...")

        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            print("  ✓ PyInstaller安装成功")
            return True
        else:
            print("  ✗ PyInstaller安装失败")
            print(f"  错误: {result.stderr}")
            return False


def build_executable():
    """打包为可执行文件"""
    print_step("步骤 3/5: 打包为可执行文件")

    script_name = "complete_exporter.py"
    exe_name = "complete_exporter"
    icon_file = "cut.ico"

    if not os.path.exists(script_name):
        print(f"  ✗ 错误: {script_name} 不存在")
        return False

    # 检查图标文件
    if os.path.exists(icon_file):
        print(f"  ✓ 使用自定义图标: {icon_file}")
    else:
        print("  ⚠ 未找到图标文件，将使用默认图标")
        icon_file = None

    print(f"  正在打包: {script_name}")
    print("  配置:")
    print("    - 单文件模式: --onefile")
    print("    - 无控制台窗口: --noconsole (Windows)")
    print("    - 输出名称: complete_exporter")
    print("    - 隐藏导入: pysrt, psutil")
    if icon_file:
        print(f"    - 自定义图标: {icon_file}")
    print()

    # PyInstaller命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                    # 打包为单个文件
        "--name", exe_name,             # 输出文件名
        "--clean",                      # 清理临时文件
        "--hidden-import", "pysrt",     # 确保pysrt库被打包
        "--hidden-import", "psutil"     # 确保psutil库被打包
    ]

    # 添加图标（如果存在）
    if icon_file:
        cmd.extend(["--icon", icon_file])

    cmd.append(script_name)

    # Windows下不显示控制台（可选）
    # 如果需要看到Python输出，注释掉下面这行
    if sys.platform == 'win32':
        # cmd.append("--noconsole")
        pass

    print("  执行命令:")
    print(f"    {' '.join(cmd)}")
    print()

    # 执行打包
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print()
        print("  ✓ 打包成功")
        return True
    else:
        print()
        print("  ✗ 打包失败")
        return False


def verify_output():
    """验证打包输出"""
    print_step("步骤 4/5: 验证打包输出")

    exe_name = "complete_exporter.exe" if sys.platform == 'win32' else "complete_exporter"
    exe_path = Path("dist") / exe_name

    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ 可执行文件已生成")
        print(f"  路径: {exe_path.absolute()}")
        print(f"  大小: {size_mb:.2f} MB")

        # 测试可执行文件
        print()
        print("  测试可执行文件...")
        result = subprocess.run(
            [str(exe_path), "--help"],
            capture_output=True,
            text=True,
            errors='ignore'
        )

        if result.returncode == 0:
            print("  ✓ 可执行文件运行正常")
        else:
            print("  ⚠ 可执行文件测试失败 (这可能是正常的,因为需要--config参数)")

        return True
    else:
        print(f"  ✗ 可执行文件未找到: {exe_path}")
        return False


def main():
    """主函数"""
    print("=" * 60)
    print("  完整导出工具 - 一键打包脚本")
    print("  将 complete_exporter.py 打包为独立可执行文件")
    print("=" * 60)

    # 检查是否在虚拟环境中
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        print("✓ 检测到虚拟环境")
        print(f"  Python: {sys.executable}")
    else:
        print("⚠ 警告: 未检测到虚拟环境")
        print(f"  Python: {sys.executable}")
        response = input("  是否继续? (y/n): ")
        if response.lower() != 'y':
            print("  取消打包")
            return 1

    # 步骤1: 清理旧数据
    clean_old_build()

    # 步骤2: 检查依赖
    if not check_dependencies():
        print("\n✗ 打包失败: 依赖安装失败")
        return 1

    # 步骤3: 打包
    if not build_executable():
        print("\n✗ 打包失败")
        return 1

    # 步骤4: 验证
    if not verify_output():
        print("\n✗ 打包失败: 可执行文件未生成")
        return 1

    # 步骤5: 完成 - 自动复制exe到当前目录
    print_step("步骤 5/5: 完成打包")

    exe_name = "complete_exporter.exe" if sys.platform == 'win32' else "complete_exporter"
    source_exe = Path("dist") / exe_name
    target_exe = Path(".") / exe_name

    print()
    print("  自动复制exe到当前目录...")
    try:
        shutil.copy2(source_exe, target_exe)
        print(f"  ✓ 已复制: {target_exe.absolute()}")
    except Exception as e:
        print(f"  ⚠ 复制失败: {e}")
        print(f"  请手动复制: {source_exe} -> {target_exe}")

    print()
    print("  生成的文件:")
    print(f"    - {target_exe.absolute()}")
    print()
    print("  使用说明:")
    print("    1. complete_exporter.exe 已在当前目录")
    print("    2. Aegisub会优先使用exe版本(如果存在)")
    print("    3. 在Aegisub中测试完整导出功能")
    print()
    print("  可以清理的临时文件:")
    print("    - build/ 目录")
    print("    - dist/ 目录")
    print("    - *.spec 文件")
    print()

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n✗ 用户取消打包")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ 打包过程出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
