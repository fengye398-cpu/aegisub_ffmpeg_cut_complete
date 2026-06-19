#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整导出处理器 - 后端脚本
照搬外部脚本的所有切割和字幕生成逻辑
"""

import os
import sys
import json
import subprocess
import argparse
import ctypes
import pysrt
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing

# 尝试导入psutil用于RAM检测
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# Windows编码兼容性处理
# 不修改sys.stdout以避免干扰退出码,而是在print时处理编码问题
# 如果需要输出特殊字符,确保只使用ASCII兼容的字符(如[成功]而不是✓)


def prevent_sleep():
    """阻止系统休眠（处理期间）"""
    if sys.platform != 'win32':
        return False  # 非Windows系统，不处理

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        if result == 0:
            print("  [提示] 防休眠设置失败（不影响处理）")
            return False
        return True
    except Exception as e:
        print(f"  [提示] 防休眠设置失败: {e}（不影响处理）")
        return False


def calculate_optimal_workers(operation_type='video'):
    """
    根据可用RAM和CPU核心数计算最优worker数量

    Args:
        operation_type: 'video' (视频编码，内存密集) 或 'light' (音频/stream copy，轻量级)

    Returns:
        int: 最优worker数量
    """
    cpu_count = multiprocessing.cpu_count()

    if not HAS_PSUTIL:
        # 没有psutil，使用保守默认值
        if operation_type == 'video':
            return min(3, max(2, cpu_count // 2))
        else:
            return min(6, cpu_count)

    # 获取可用RAM（GB）
    available_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
    total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)

    print(f"  [系统资源] 总内存: {total_ram_gb:.1f}GB, 可用: {available_ram_gb:.1f}GB")

    if operation_type == 'video':
        # 视频编码：每个worker约250-300MB，使用40%可用RAM
        # 保留至少800MB给系统
        usable_ram_gb = max(0, available_ram_gb - 0.8)
        # 使用40%可用RAM来最大化利用率，同时保持稳定性
        target_ram_gb = usable_ram_gb * 0.4
        # 每个worker按300MB计算（保守估计）
        workers = int(target_ram_gb * 1024 / 300)
        # 限制范围：最少2个，最多不超过CPU核心数和8
        workers = max(2, min(workers, cpu_count, 8))

        print(f"  [自动调整] 视频编码worker: {workers}个 (基于{available_ram_gb:.1f}GB可用内存)")
        return workers
    else:
        # 轻量级操作（音频/stream copy）：每个worker约50MB
        usable_ram_gb = max(0, available_ram_gb - 0.5)
        target_ram_gb = usable_ram_gb * 0.3
        workers = int(target_ram_gb * 1024 / 50)
        workers = max(4, min(workers, cpu_count, 12))

        print(f"  [自动调整] 轻量级操作worker: {workers}个")
        return workers


def allow_sleep():
    """恢复系统休眠（处理完成后）"""
    if sys.platform != 'win32':
        return False

    ES_CONTINUOUS = 0x80000000

    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        return True
    except:
        return False


class CompleteExporter:
    """完整导出处理器"""

    def __init__(self, config_file):
        """初始化"""
        with open(config_file, 'r', encoding='utf-8') as f:
            self.config = json.load(f)

        self.video_path = self.config['video_path']
        self.segments = self.config['segments']
        self.mode = self.config['mode']  # fast / reencode / continuous
        self.naming = self.config['naming']
        self.crf = self.config.get('crf', 24)
        self.preset = self.config.get('preset', 'veryfast')
        self.gap = self.config.get('gap', 200)

        # 检测连续模式
        self.is_continuous = (self.mode == 'continuous')

        # 检测跟读模式
        self.is_shadowing = (self.mode == 'shadowing')
        self.shadowing_params = self.config.get('shadowing', {})
        self.show_pause_subtitles = self.shadowing_params.get('show_pause_subtitles', True)  # 默认显示


        # 检测媒体类型
        self.media_type = self._detect_media_type()
        self.is_audio_only = (self.media_type == 'audio')

        # 生成输出目录（与外部脚本一致）
        video_dir = Path(self.video_path).parent
        video_name = Path(self.video_path).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 外部脚本目录结构：video_name_timestamp/video_name/
        base_dir_name = f"{video_name}_{timestamp}"
        self.base_dir = video_dir / base_dir_name  # 一级目录（带时间戳，存放合并文件）
        self.chunk_dir = self.base_dir / video_name  # 二级目录（存放片段文件）

        # 创建目录
        self.chunk_dir.mkdir(parents=True, exist_ok=True)

        print(f"媒体类型: {'纯音频' if self.is_audio_only else '视频'}")
        print(f"输出目录: {self.base_dir}")
        print(f"片段目录: {self.chunk_dir}")

        # 显示过短片段详细信息（如果有）
        self._print_short_segments_details()

        # 检查FFmpeg是否可用
        self._check_ffmpeg()

        # 提示psutil状态
        if not HAS_PSUTIL:
            print("  [提示] 未安装psutil，使用默认并行配置")
            print("  [提示] 安装psutil可自动优化性能: pip install psutil")

    def _write_error_file(self, error_msg):
        """写入错误信息到文件，供Lua读取"""
        try:
            # 优先使用临时目录（避免权限问题）
            temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
            error_file = os.path.join(temp_dir, "last_error.txt")
            with open(error_file, 'w', encoding='utf-8') as f:
                f.write(error_msg)
            print(f"  [调试] 错误信息已写入: {error_file}")
        except Exception as e:
            print(f"  [警告] 无法写入错误文件到临时目录: {e}")

            # Fallback: 尝试脚本目录（兼容旧版本）
            try:
                if getattr(sys, 'frozen', False):
                    script_dir = os.path.dirname(sys.executable)
                else:
                    script_dir = os.path.dirname(os.path.abspath(__file__))

                error_file = os.path.join(script_dir, "last_error.txt")
                with open(error_file, 'w', encoding='utf-8') as f:
                    f.write(error_msg)
                print(f"  [调试] 错误信息已写入（脚本目录）: {error_file}")
            except Exception as e2:
                print(f"  [警告] 无法写入错误文件到脚本目录: {e2}")

    def _check_ffmpeg(self):
        """检查FFmpeg和FFprobe是否可用"""
        print("\n检查FFmpeg...")

        # 检查ffmpeg
        try:
            result = subprocess.run(
                ['ffmpeg', '-version'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=5
            )
            if result.returncode == 0:
                # 提取版本号（第一行通常包含版本信息）
                version_line = result.stdout.split('\n')[0]
                print(f"  [成功] FFmpeg已安装: {version_line}")
            else:
                raise Exception("ffmpeg返回错误")
        except FileNotFoundError:
            self._print_ffmpeg_error()
            sys.exit(1)
        except Exception as e:
            print(f"  [警告] FFmpeg检测异常: {e}")
            self._print_ffmpeg_error()
            sys.exit(1)

        # 检查ffprobe
        try:
            result = subprocess.run(
                ['ffprobe', '-version'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=5
            )
            if result.returncode == 0:
                print(f"  [成功] FFprobe已安装")
            else:
                raise Exception("ffprobe返回错误")
        except FileNotFoundError:
            self._print_ffprobe_error()
            sys.exit(1)
        except Exception as e:
            print(f"  [警告] FFprobe检测异常: {e}")
            self._print_ffprobe_error()
            sys.exit(1)

        print("  [成功] FFmpeg环境检查通过\n")

    def _print_ffmpeg_error(self):
        """打印FFmpeg缺失错误信息"""
        error_msg = """
============================================================
[错误] 未找到FFmpeg
============================================================

本工具需要FFmpeg才能运行。

请按照以下步骤安装FFmpeg:

方法1: 使用包管理器
  Windows (使用Chocolatey):
    choco install ffmpeg

方法2: 手动下载（推荐）
  1. 访问 https://ffmpeg.org/download.html
  2. 下载适合您系统的FFmpeg
  3. 解压并将ffmpeg.exe添加到系统PATH

方法3: 便携版
  将ffmpeg.exe和ffprobe.exe放在与本工具相同的目录下

============================================================
"""
        print(error_msg)

        # 同时写入error.txt供Lua读取
        self._write_error_file(error_msg)

    def _print_ffprobe_error(self):
        """打印FFprobe缺失错误信息"""
        error_msg = """
============================================================
[错误] 未找到FFprobe
============================================================

FFprobe是FFmpeg的一部分，通常会随FFmpeg一起安装。
如果您已安装FFmpeg但仍看到此错误，请确保:
  1. ffprobe.exe在系统PATH中
  2. 或将ffprobe.exe放在与本工具相同的目录下

============================================================
"""
        print(error_msg)

        # 同时写入error.txt供Lua读取
        self._write_error_file(error_msg)

    def _print_short_segments_details(self):
        """显示过短片段的详细信息到CMD (仅显示SRT序号和时间轴)"""
        # 读取配置中的 short_segments_info
        short_info = self.config.get('short_segments_info')

        if not short_info or not short_info.get('detected'):
            return  # 没有过短片段或未检测到，不输出

        print("\n" + "=" * 60)
        print("【过短片段详细信息】")
        print("=" * 60)
        print(f"以下 {short_info['short_count']} 个过短片段已被过滤（不会被切割）:")
        print()

        # 显示每个过短片段的详细信息(从details数组读取)
        details = short_info.get('details', [])
        for i, detail in enumerate(details, 1):
            srt_number = detail.get('srt_number', '?')
            time_range = detail.get('time_range', '?')
            duration_ms = detail.get('duration', 0)

            print(f"片段 #{i} - SRT序号: {srt_number}")
            print(f"  时间轴: {time_range}")
            print(f"  时长: {duration_ms:.0f}ms")
            print()

        print("=" * 60)
        print(f"过滤结果:")
        print(f"  原始片段数: {short_info['total_count']} 个")
        print(f"  过滤片段数: {short_info['short_count']} 个")
        print(f"  剩余片段数: {short_info['remaining_count']} 个")
        print("=" * 60)
        print()

    def _detect_media_type(self):
        """检测媒体文件类型"""
        file_ext = Path(self.video_path).suffix.lower()

        # 音频格式
        audio_exts = ['.mp3', '.wav', '.flac', '.aac', '.m4a', '.ogg', '.wma']
        # 视频格式
        video_exts = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm']

        if file_ext in audio_exts:
            return 'audio'
        elif file_ext in video_exts:
            return 'video'
        else:
            # 默认按视频处理
            print(f"  [警告] 未知文件格式 {file_ext}，按视频处理")
            return 'video'

    def run(self):
        """执行完整导出流程"""
        # 阻止系统休眠（处理期间）
        prevent_sleep()

        try:
            print("\n" + "=" * 60)
            print("开始切割")
            print("=" * 60)

            if self.is_shadowing:
                # ========== 跟读练习模式 ==========
                print(f"模式: 跟读练习")
                print(f"播放次数: {self.shadowing_params.get('repeat_count', 3)}次")
                print(f"重复间停顿: {'启用' if self.shadowing_params.get('pause_between_repeats', True) else '禁用'}")
                pause_mode = self.shadowing_params.get('pause_mode', 'multiplier')
                if pause_mode == 'multiplier':
                    print(f"停顿时长: 句子时长 × {self.shadowing_params.get('pause_multiplier', 1.5)}倍")
                else:
                    print(f"停顿时长: {self.shadowing_params.get('pause_seconds', 2.0)}秒")
                print(f"停顿时显示字幕: {'启用' if self.shadowing_params.get('show_pause_subtitles', True) else '禁用'}")
                print(f"CRF质量: {self.crf}")
                print(f"编码预设: {self.preset}")

                if self.is_audio_only:
                    # 纯音频跟读模式：3个阶段
                    print("\n【阶段 1/3】切割音频片段...")
                    self.cut_audio_segments()

                    print("\n【阶段 2/3】生成跟读练习音频...")
                    self.generate_shadowing_audio()

                    print("\n【阶段 3/3】生成跟读练习字幕...")
                    self.generate_shadowing_subtitles()
                else:
                    # 视频跟读模式：5个阶段
                    print("\n【阶段 1/5】切割视频片段...")
                    self.cut_video_segments()

                    print("\n【阶段 2/5】提取音频片段...")
                    self.extract_audio_segments()

                    print("\n【阶段 3/5】生成跟读练习视频...")
                    self.generate_shadowing_video()

                    print("\n【阶段 4/5】生成跟读练习音频...")
                    self.generate_shadowing_audio()

                    print("\n【阶段 5/5】生成跟读练习字幕...")
                    self.generate_shadowing_subtitles()

            elif self.is_continuous:
                # ========== 连续切割模式 ==========
                print(f"模式: 连续切割")
                print(f"从第一条字幕到最后一条字幕切割完整片段")

                if self.is_audio_only:
                    # 纯音频连续模式：2个阶段
                    print("\n【阶段 1/2】切割连续音频片段...")
                    self.cut_continuous_audio()

                    print("\n【阶段 2/2】生成字幕文件...")
                    self.generate_continuous_subtitles()
                else:
                    # 视频连续模式：3个阶段
                    print("\n【阶段 1/3】切割连续视频片段...")
                    self.cut_continuous_video()

                    print("\n【阶段 2/3】提取音频...")
                    self.extract_audio_from_continuous()

                    print("\n【阶段 3/3】生成字幕文件...")
                    self.generate_continuous_subtitles()

            elif self.is_audio_only:
                # ========== 纯音频片段模式 ==========
                mode_display = "快速模式" if self.mode == "fast" else "重新编码模式"
                print(f"模式: {mode_display}")

                # 纯音频模式：3个阶段
                # 阶段1: 切割音频片段
                print("\n【阶段 1/3】切割音频片段...")
                self.cut_audio_segments()

                # 阶段2: 生成片段字幕
                print("\n【阶段 2/3】生成片段字幕...")
                self.generate_segment_subtitles()

                # 阶段3: 合并音频并生成完整字幕
                print("\n【阶段 3/3】合并音频并生成完整字幕...")
                self.merge_audio_only()
                self.generate_merged_subtitles()
            else:
                # ========== 视频片段模式 ==========
                mode_display = "快速模式" if self.mode == "fast" else "重新编码模式"
                print(f"模式: {mode_display}")
                if self.mode == "reencode":
                    print(f"CRF质量: {self.crf}")
                    print(f"编码预设: {self.preset}")

                # 视频模式：5个阶段
                # 阶段1: 切割视频片段
                print("\n【阶段 1/5】切割视频片段...")
                self.cut_video_segments()

                # 阶段2: 提取音频片段
                print("\n【阶段 2/5】提取音频片段...")
                self.extract_audio_segments()

                # 阶段3: 生成片段字幕
                print("\n【阶段 3/5】生成片段字幕...")
                self.generate_segment_subtitles()

                # 阶段4: 合并视频和音频
                print("\n【阶段 4/5】合并视频和音频...")
                self.merge_video_audio()

                # 阶段5: 生成完整字幕
                print("\n【阶段 5/5】生成完整字幕...")
                self.generate_merged_subtitles()

            print("\n" + "=" * 60)
            print("[成功] 完整导出成功！")
            print(f"输出位置: {self.base_dir}")
            print("=" * 60)

            # 确保所有输出都被刷新
            sys.stdout.flush()
            sys.stderr.flush()

            # 将输出路径写入临时文件供Lua读取
            try:
                # 优先使用临时目录（避免权限问题）
                temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
                output_path_file = os.path.join(temp_dir, "last_output_path.txt")
                with open(output_path_file, 'w', encoding='utf-8') as f:
                    f.write(str(self.base_dir))
                print(f"  [调试] 输出路径已写入: {output_path_file}")
            except Exception as e:
                print(f"  [警告] 无法写入输出路径到临时目录: {e}")

                # Fallback: 尝试脚本目录（兼容旧版本）
                try:
                    if getattr(sys, 'frozen', False):
                        script_dir = os.path.dirname(sys.executable)
                    else:
                        script_dir = os.path.dirname(os.path.abspath(__file__))

                    output_path_file = os.path.join(script_dir, "last_output_path.txt")
                    with open(output_path_file, 'w', encoding='utf-8') as f:
                        f.write(str(self.base_dir))
                    print(f"  [调试] 输出路径已写入（脚本目录）: {output_path_file}")
                except Exception as e2:
                    print(f"  [警告] 无法写入输出路径到脚本目录: {e2}")

            return 0

        except Exception as e:
            print(f"\n[失败] 导出失败: {e}")
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            return 1

        finally:
            # 恢复系统休眠（无论成功或失败）
            allow_sleep()

    def _cut_single_video_segment(self, seg, index, total):
        """切割单个视频片段（用于并行处理）"""
        output_file = self.chunk_dir / f"{seg['filename']}.mp4"
        start_time = seg['start_time'] / 1000.0  # 毫秒转秒
        end_time = seg['end_time'] / 1000.0
        duration = end_time - start_time

        print(f"  [{index}/{total}] 切割 {seg['filename']}.mp4 ({start_time:.2f}s - {end_time:.2f}s)")

        if self.mode == 'fast':
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(start_time),
                '-i', self.video_path,
                '-t', str(duration),
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '24',
                '-c:a', 'aac',
                '-ac', '2',
                '-b:a', '192k',
                str(output_file)
            ]
        else:
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(start_time),
                '-i', self.video_path,
                '-t', str(duration),
                '-c:v', 'libx264',
                '-preset', self.preset,
                '-crf', str(self.crf),
                '-c:a', 'aac',
                '-ac', '2',
                '-b:a', '192k',
                str(output_file)
            ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        if result.returncode != 0:
            print(f"    [警告] 切割失败")
            if result.stderr:
                print(f"    错误: {result.stderr[-500:]}")  # 显示最后500字符（包含实际错误）

        return seg['index']

    def cut_video_segments(self):
        """切割视频片段（并行处理）"""
        total = len(self.segments)
        max_workers = calculate_optimal_workers('video')

        print(f"  使用 {max_workers} 个并行worker处理 {total} 个片段")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for i, seg in enumerate(self.segments, 1):
                future = executor.submit(self._cut_single_video_segment, seg, i, total)
                futures.append((future, seg['index']))

            # 等待所有任务完成（按提交顺序）
            for future, seg_index in futures:
                try:
                    future.result()
                except Exception as e:
                    print(f"    [错误] 切割任务失败 (片段{seg_index}): {e}")

        # 确保所有文件I/O完成
        import time
        time.sleep(0.1)

    def _cut_single_audio_segment(self, seg, index, total, output_ext):
        """切割单个音频片段（用于并行处理）"""
        output_file = self.chunk_dir / f"{seg['filename']}{output_ext}"
        start_time = seg['start_time'] / 1000.0
        end_time = seg['end_time'] / 1000.0
        duration = end_time - start_time

        print(f"  [{index}/{total}] 切割 {seg['filename']}{output_ext} ({start_time:.2f}s - {end_time:.2f}s)")

        if self.mode == 'fast':
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(start_time),
                '-i', self.video_path,
                '-t', str(duration),
                '-c:a', 'libmp3lame',
                '-b:a', '192k',
                str(output_file)
            ]
        else:
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(start_time),
                '-i', self.video_path,
                '-t', str(duration),
                '-c:a', 'libmp3lame',
                '-b:a', '192k',
                str(output_file)
            ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        if result.returncode != 0:
            print(f"    [警告] 切割失败")
            if result.stderr:
                print(f"    错误: {result.stderr[-500:]}")  # 显示最后500字符（包含实际错误）

        return seg['index']

    def cut_audio_segments(self):
        """切割纯音频片段（并行处理）"""
        total = len(self.segments)
        max_workers = calculate_optimal_workers('light')

        input_ext = Path(self.video_path).suffix.lower()
        output_ext = input_ext if input_ext in ['.mp3', '.wav', '.flac'] else '.mp3'

        print(f"  使用 {max_workers} 个并行worker处理 {total} 个片段")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for i, seg in enumerate(self.segments, 1):
                future = executor.submit(self._cut_single_audio_segment, seg, i, total, output_ext)
                futures.append((future, seg['index']))

            # 等待所有任务完成（按提交顺序）
            for future, seg_index in futures:
                try:
                    future.result()
                except Exception as e:
                    print(f"    [错误] 切割任务失败 (片段{seg_index}): {e}")

        # 确保所有文件I/O完成
        import time
        time.sleep(0.1)

    def extract_audio_segments(self):
        """从视频片段中提取音频"""
        import json
        total = len(self.segments)

        # 缓存视频时长以减少ffprobe调用
        self.video_durations_cache = {}

        for i, seg in enumerate(self.segments, 1):
            video_file = self.chunk_dir / f"{seg['filename']}.mp4"
            audio_file = self.chunk_dir / f"{seg['filename']}.mp3"

            if not video_file.exists():
                print(f"  [{i}/{total}] 跳过 {seg['filename']} (视频不存在)")
                continue

            print(f"  [{i}/{total}] 提取 {seg['filename']}.mp3")

            # 获取视频文件的精确时长并缓存
            duration = float(json.loads(subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(video_file)
            ], capture_output=True, text=True).stdout)['format']['duration'])

            self.video_durations_cache[seg['index']] = duration

            # 统一使用MP3格式（快速）
            cmd = [
                'ffmpeg', '-y',
                '-i', str(video_file),
                '-vn',
                '-acodec', 'libmp3lame',
                '-b:a', '192k',
                str(audio_file)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )

            if result.returncode != 0:
                print(f"    [警告] 音频提取失败")

    def generate_segment_subtitles(self):
        """生成片段字幕文件"""
        total = len(self.segments)

        for i, seg in enumerate(self.segments, 1):
            srt_file = self.chunk_dir / f"{seg['filename']}.srt"

            print(f"  [{i}/{total}] 生成 {seg['filename']}.srt")

            # 片段字幕从00:00:00开始
            duration_ms = seg['duration']

            with open(srt_file, 'w', encoding='utf-8') as f:
                f.write("1\n")
                f.write(f"00:00:00,000 --> {self.ms_to_srt(duration_ms)}\n")
                f.write(seg['text'].replace('\\N', '\n') + "\n\n")

    def merge_video_audio(self):
        """合并视频和音频文件"""
        import shutil
        import tempfile

        video_name = Path(self.video_path).stem

        # 收集实际存在的视频文件并按数字排序
        print(f"  收集视频文件...")
        # 按照self.segments的顺序收集文件，确保顺序正确
        video_files = []
        for seg in self.segments:
            video_file = self.chunk_dir / f"{seg['filename']}.mp4"
            if video_file.exists():
                video_files.append(video_file)

        # 创建临时目录，使用简单文件名（解决FFmpeg concat的文件名问题）
        # 使用系统临时目录（保证有写入权限，通常在SSD上速度快）
        print(f"  创建临时目录...")
        temp_video_dir = Path(tempfile.mkdtemp(prefix="aegisub_video_"))

        print(f"  复制视频到临时目录（使用简单文件名）...")
        temp_video_files = []
        for i, video_file in enumerate(video_files, 1):
            # 使用简单文件名：0001.mp4, 0002.mp4, ...
            temp_name = f"{i:04d}.mp4"
            temp_file = temp_video_dir / temp_name
            shutil.copy2(video_file, temp_file)
            temp_video_files.append(temp_file)

        # 合并视频
        video_list_file = self.base_dir / "video_concat_list.txt"
        merged_video = self.base_dir / f"{video_name}_video.mp4"

        print(f"  生成视频合并列表...")
        with open(video_list_file, 'w', encoding='utf-8') as f:
            for temp_file in temp_video_files:
                # 使用相对路径
                escaped_path = self._escape_ffmpeg_path(temp_file, base_dir=self.base_dir)
                f.write(f"file '{escaped_path}'\n")

        print(f"  合并视频...")
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(video_list_file),
            '-c', 'copy',
            str(merged_video)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        # 检查FFmpeg执行结果
        if result.returncode != 0:
            print(f"    [错误] 视频合并失败 (返回码: {result.returncode})")
            print(f"    [错误] concat列表保留在: {video_list_file}")
            if result.stderr:
                # 只打印stderr的前500字符，避免输出过长
                error_msg = result.stderr[:500]
                print(f"    [错误] FFmpeg错误信息:\n{error_msg}")
            # 不删除concat列表，方便调试
            raise Exception(f"视频合并失败，请检查: {video_list_file}")

        video_list_file.unlink()  # 成功后才删除临时文件

        # 清理临时视频目录
        print(f"  清理临时视频目录...")
        shutil.rmtree(temp_video_dir)

        print(f"    [成功] 视频合并完成: {len(video_files)} 个文件")

        # 合并音频
        audio_list_file = self.base_dir / "audio_concat_list.txt"
        merged_audio = self.base_dir / f"{video_name}_audio.mp3"

        print(f"  收集音频文件...")
        # 按照self.segments的顺序收集文件，确保顺序正确
        audio_files = []
        for seg in self.segments:
            audio_file = self.chunk_dir / f"{seg['filename']}.mp3"
            if audio_file.exists():
                audio_files.append(audio_file)

        # 创建临时目录，使用简单文件名
        # 使用系统临时目录（保证有写入权限）
        print(f"  创建临时目录...")
        temp_audio_dir = Path(tempfile.mkdtemp(prefix="aegisub_audio_"))

        print(f"  复制音频到临时目录（使用简单文件名）...")
        temp_audio_files = []
        for i, audio_file in enumerate(audio_files, 1):
            temp_name = f"{i:04d}.mp3"
            temp_file = temp_audio_dir / temp_name
            shutil.copy2(audio_file, temp_file)
            temp_audio_files.append(temp_file)

        print(f"  生成音频合并列表...")
        with open(audio_list_file, 'w', encoding='utf-8') as f:
            for temp_file in temp_audio_files:
                escaped_path = self._escape_ffmpeg_path(temp_file, base_dir=self.base_dir)
                f.write(f"file '{escaped_path}'\n")

        print(f"  合并音频...")
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(audio_list_file),
            '-c', 'copy',
            str(merged_audio)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        # 检查FFmpeg执行结果
        if result.returncode != 0:
            print(f"    [错误] 音频合并失败 (返回码: {result.returncode})")
            print(f"    [错误] concat列表保留在: {audio_list_file}")
            if result.stderr:
                error_msg = result.stderr[:500]
                print(f"    [错误] FFmpeg错误信息:\n{error_msg}")
            raise Exception(f"音频合并失败，请检查: {audio_list_file}")

        audio_list_file.unlink()  # 成功后才删除临时文件

        # 清理临时音频目录
        print(f"  清理临时音频目录...")
        shutil.rmtree(temp_audio_dir)

        print(f"    [成功] 音频合并完成: {len(audio_files)} 个文件")

    def merge_audio_only(self):
        """合并纯音频文件"""
        import shutil
        import tempfile

        media_name = Path(self.video_path).stem

        # 合并音频（统一使用mp3格式）
        audio_list_file = self.base_dir / "audio_concat_list.txt"
        merged_audio = self.base_dir / f"{media_name}_audio.mp3"

        print(f"  收集音频文件...")
        # 检测输入音频格式
        input_ext = Path(self.video_path).suffix.lower()
        seg_ext = input_ext if input_ext in ['.mp3', '.wav', '.flac'] else '.mp3'

        # 按照self.segments的顺序收集文件，确保顺序正确
        audio_files = []
        for seg in self.segments:
            audio_file = self.chunk_dir / f"{seg['filename']}{seg_ext}"
            if audio_file.exists():
                audio_files.append(audio_file)

        # 创建临时目录，使用简单文件名
        # 使用系统临时目录（保证有写入权限）
        print(f"  创建临时目录...")
        temp_audio_dir = Path(tempfile.mkdtemp(prefix="aegisub_audio_only_"))

        print(f"  复制音频到临时目录（使用简单文件名）...")
        temp_audio_files = []
        for i, audio_file in enumerate(audio_files, 1):
            temp_name = f"{i:04d}{seg_ext}"
            temp_file = temp_audio_dir / temp_name
            shutil.copy2(audio_file, temp_file)
            temp_audio_files.append(temp_file)

        print(f"  生成音频合并列表...")
        with open(audio_list_file, 'w', encoding='utf-8') as f:
            for temp_file in temp_audio_files:
                escaped_path = self._escape_ffmpeg_path(temp_file, base_dir=self.base_dir)
                f.write(f"file '{escaped_path}'\n")

        print(f"  合并音频...")
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(audio_list_file),
            '-c', 'copy',
            str(merged_audio)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        # 检查FFmpeg执行结果
        if result.returncode != 0:
            print(f"    [错误] 音频合并失败 (返回码: {result.returncode})")
            print(f"    [错误] concat列表保留在: {audio_list_file}")
            if result.stderr:
                error_msg = result.stderr[:500]
                print(f"    [错误] FFmpeg错误信息:\n{error_msg}")
            raise Exception(f"音频合并失败，请检查: {audio_list_file}")

        audio_list_file.unlink()  # 成功后才删除临时文件

        # 清理临时音频目录
        print(f"  清理临时音频目录...")
        shutil.rmtree(temp_audio_dir)

        print(f"    [成功] 音频合并完成: {len(audio_files)} 个文件")

    def generate_merged_subtitles(self):
        """生成完整字幕文件"""
        media_name = Path(self.video_path).stem

        if self.is_audio_only:
            # 纯音频模式：只生成一个字幕文件（与音频文件同名）
            subtitle_srt = self.base_dir / f"{media_name}_audio.srt"
            print(f"  生成完整字幕: {subtitle_srt.name}")
            self._generate_subtitle_with_probe(subtitle_srt, is_audio=True)
        else:
            # 视频模式：生成视频版+音频版
            # 生成视频版字幕
            video_srt = self.base_dir / f"{media_name}_video.srt"
            print(f"  生成视频版字幕: {video_srt.name}")
            self._generate_subtitle_with_probe(video_srt, is_audio=False)

            # 生成音频版字幕
            audio_srt = self.base_dir / f"{media_name}_audio.srt"
            print(f"  生成音频版字幕: {audio_srt.name}")
            self._generate_subtitle_with_probe(audio_srt, is_audio=True)

    def _generate_subtitle_with_probe(self, output_file, is_audio):
        """生成字幕（完全照搬外部脚本逻辑 - 读取每个片段的SRT文件）"""

        # 确定文件扩展名
        if self.is_audio_only:
            # 纯音频模式：使用输入音频格式
            input_ext = Path(self.video_path).suffix.lower()
            file_ext = input_ext if input_ext in ['.mp3', '.wav', '.flac'] else '.mp3'
            file_ext = file_ext.lstrip('.')  # 去掉点号
        else:
            # 视频模式：mp4或mp3
            file_ext = 'mp3' if is_audio else 'mp4'

        # 初始化（照搬外部脚本）
        merged_subs = []  # 存储所有合并后的字幕
        current_time = timedelta(seconds=0)  # 累计时间
        gap = 0.2  # 标准gap（200ms）
        gap_td = timedelta(seconds=gap)

        print(f"开始合并字幕...")

        # 确保所有文件存在且可读（等待文件系统同步）
        import time
        import os
        max_retries = 3
        for seg in self.segments:
            media_file = self.chunk_dir / f"{seg['filename']}.{file_ext}"
            srt_file = self.chunk_dir / f"{seg['filename']}.srt"

            for retry in range(max_retries):
                if media_file.exists() and srt_file.exists():
                    # 确保文件可读（等待文件系统缓存刷新）
                    try:
                        os.stat(str(media_file))
                        os.stat(str(srt_file))
                        break
                    except:
                        if retry < max_retries - 1:
                            time.sleep(0.05)
                else:
                    if retry < max_retries - 1:
                        time.sleep(0.05)

        # 遍历每个片段（照搬外部脚本）
        for i, seg in enumerate(self.segments):
            media_file = self.chunk_dir / f"{seg['filename']}.{file_ext}"
            srt_file = self.chunk_dir / f"{seg['filename']}.srt"

            # 检查SRT文件是否存在
            if not srt_file.exists():
                print(f"    [警告] 字幕文件不存在，跳过片段 {seg['index']}: {srt_file.name}")
                continue

            # 读取片段的字幕文件（照搬外部脚本）
            try:
                subs = pysrt.open(str(srt_file), encoding='utf-8-sig')
            except Exception as e:
                print(f"    [警告] 无法读取字幕文件 {srt_file.name}: {e}")
                continue

            # 遍历片段内的每条字幕（照搬外部脚本）
            for sub in subs:
                # 提取原始字幕的时间（片段内的相对时间）
                sub_start = timedelta(
                    hours=sub.start.hours,
                    minutes=sub.start.minutes,
                    seconds=sub.start.seconds,
                    milliseconds=sub.start.milliseconds
                )
                sub_end = timedelta(
                    hours=sub.end.hours,
                    minutes=sub.end.minutes,
                    seconds=sub.end.seconds,
                    milliseconds=sub.end.milliseconds
                )

                # 调整时间轴：累计时间 + 片段内相对时间（照搬外部脚本）
                new_start = current_time + sub_start
                new_end = current_time + sub_end

                # 智能gap处理（防止字幕重叠）（照搬外部脚本）
                if merged_subs:
                    prev_end = merged_subs[-1]['end']
                    if new_start < prev_end + gap_td:
                        new_start = prev_end + gap_td
                        if new_end < new_start:
                            new_end = new_start + timedelta(milliseconds=500)

                # 添加到合并列表
                merged_subs.append({
                    'index': len(merged_subs) + 1,
                    'start': new_start,
                    'end': new_end,
                    'text': sub.text
                })

            # 使用ffprobe获取实际媒体文件时长（照搬外部脚本）
            duration_sec = self.get_media_duration(str(media_file))
            if duration_sec is None:
                print(f"    [警告] 无法获取文件时长，跳过片段 {seg['index']}")
                continue

            # 推进累计时间（照搬外部脚本）- duration_sec已经是秒数，直接使用
            current_time += timedelta(seconds=duration_sec)

            # 每100个片段打印一次进度
            if (i + 1) % 100 == 0 or (i + 1) == len(self.segments):
                print(f"    处理进度: {i+1}/{len(self.segments)}, 累计时间: {current_time.total_seconds():.2f}s")

        # 写入合并后的字幕文件（照搬外部脚本）
        print(f"    写入最终字幕文件...")
        with open(output_file, 'w', encoding='utf-8') as f:
            for sub in merged_subs:
                f.write(f"{sub['index']}\n")
                f.write(f"{self._format_timedelta(sub['start'])} --> {self._format_timedelta(sub['end'])}\n")
                f.write(f"{sub['text']}\n\n")

        print(f"    [成功] 字幕合并成功: {len(merged_subs)} 条字幕")
        print(f"    [成功] 最终时间轴长度: {current_time.total_seconds():.2f}s")

    def _format_timedelta(self, td):
        """格式化timedelta为SRT时间格式（照搬外部脚本）"""
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        milliseconds = int(td.microseconds / 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    def get_media_duration(self, file_path):
        """使用FFprobe获取媒体文件的实际时长（秒，float类型保持完整精度）"""
        if not os.path.exists(file_path):
            return None

        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )

            if result.returncode == 0 and result.stdout.strip():
                duration = float(result.stdout.strip())
                return duration  # 直接返回秒数（float），保持完整精度
            else:
                return None

        except Exception:
            return None

    def cut_continuous_video(self):
        """切割连续视频片段"""
        # 计算时间范围
        first_seg = self.segments[0]
        last_seg = self.segments[-1]

        start_time = first_seg['start_time'] / 1000.0  # 毫秒转秒
        end_time = last_seg['end_time'] / 1000.0
        duration = end_time - start_time

        # 固定文件名：01.mp4
        output_file = self.chunk_dir / "01.mp4"

        print(f"  切割时间范围: {start_time:.2f}s - {end_time:.2f}s")
        print(f"  总时长: {duration:.2f}s")
        print(f"  包含 {len(self.segments)} 条字幕")

        # 使用重新编码（参数从配置获取）
        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_time),
            '-i', self.video_path,
            '-t', str(duration),
            '-c:v', 'libx264',
            '-preset', self.preset,
            '-crf', str(self.crf),
            '-c:a', 'aac',
            '-b:a', '192k',
            str(output_file)
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        if result.returncode == 0:
            print(f"  [成功] 连续视频切割成功")
        else:
            print(f"  [失败] 连续视频切割失败")
            if result.stderr:
                print(f"  错误: {result.stderr[:200]}")
            raise Exception("连续视频切割失败")

    def cut_continuous_audio(self):
        """切割连续音频片段"""
        # 计算时间范围
        first_seg = self.segments[0]
        last_seg = self.segments[-1]

        start_time = first_seg['start_time'] / 1000.0  # 毫秒转秒
        end_time = last_seg['end_time'] / 1000.0
        duration = end_time - start_time

        # 检测输入音频格式
        input_ext = Path(self.video_path).suffix.lower()
        output_ext = input_ext if input_ext in ['.mp3', '.wav', '.flac'] else '.mp3'

        # 固定文件名：01.mp3 (或其他格式)
        output_file = self.chunk_dir / f"01{output_ext}"

        print(f"  切割时间范围: {start_time:.2f}s - {end_time:.2f}s")
        print(f"  总时长: {duration:.2f}s")
        print(f"  包含 {len(self.segments)} 条字幕")

        # 使用固定编码参数
        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_time),
            '-i', self.video_path,
            '-t', str(duration),
            '-c:a', 'libmp3lame',
            '-b:a', '192k',
            str(output_file)
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        if result.returncode == 0:
            print(f"  [成功] 连续音频切割成功")
        else:
            print(f"  [失败] 连续音频切割失败")
            if result.stderr:
                print(f"  错误: {result.stderr[:200]}")
            raise Exception("连续音频切割失败")

    def extract_audio_from_continuous(self):
        """从连续视频片段中提取音频"""
        video_file = self.chunk_dir / "01.mp4"
        audio_file = self.chunk_dir / "01.mp3"

        if not video_file.exists():
            print(f"  [警告] 视频文件不存在，跳过音频提取")
            return

        print(f"  提取音频: 01.mp3")

        cmd = [
            'ffmpeg', '-y',
            '-i', str(video_file),
            '-vn',
            '-acodec', 'libmp3lame',
            '-b:a', '192k',
            str(audio_file)
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        if result.returncode == 0:
            print(f"  [成功] 音频提取成功")
        else:
            print(f"  [警告] 音频提取失败")
            if result.stderr:
                print(f"  错误: {result.stderr[:200]}")

    def generate_continuous_subtitles(self):
        """生成连续模式字幕（相对时间轴）"""
        media_name = Path(self.video_path).stem

        # 计算起始时间（第一条字幕的开始时间）
        first_start_ms = self.segments[0]['start_time']

        # 生成片段字幕文件（01.srt）
        srt_file = self.chunk_dir / "01.srt"

        print(f"  生成字幕: 01.srt")
        print(f"  时间轴偏移: -{self.ms_to_srt(first_start_ms)} (转换为相对时间)")

        with open(srt_file, 'w', encoding='utf-8') as f:
            for idx, seg in enumerate(self.segments, 1):
                # 转换为相对时间：减去第一条字幕的起始时间
                new_start_ms = seg['start_time'] - first_start_ms
                new_end_ms = seg['end_time'] - first_start_ms

                # 写入SRT
                f.write(f"{idx}\n")
                f.write(f"{self.ms_to_srt(new_start_ms)} --> {self.ms_to_srt(new_end_ms)}\n")
                f.write(seg['text'].replace('\\N', '\n') + "\n\n")

        print(f"  [成功] 成功生成 {len(self.segments)} 条字幕")

        # 复制字幕到合并目录（生成视频版和音频版）
        import shutil

        if self.is_audio_only:
            # 纯音频模式：只生成音频版字幕
            merged_audio_srt = self.base_dir / f"{media_name}_audio.srt"
            shutil.copy(srt_file, merged_audio_srt)
            print(f"  [成功] 复制字幕到: {merged_audio_srt.name}")
        else:
            # 视频模式：生成视频版和音频版字幕
            merged_video_srt = self.base_dir / f"{media_name}_video.srt"
            merged_audio_srt = self.base_dir / f"{media_name}_audio.srt"

            shutil.copy(srt_file, merged_video_srt)
            shutil.copy(srt_file, merged_audio_srt)

            print(f"  [成功] 复制字幕到: {merged_video_srt.name}")
            print(f"  [成功] 复制字幕到: {merged_audio_srt.name}")

        # 复制视频/音频到合并目录（兼容现有"打开目录"逻辑）
        if self.is_audio_only:
            # 纯音频模式：复制音频文件
            input_ext = Path(self.video_path).suffix.lower()
            output_ext = input_ext if input_ext in ['.mp3', '.wav', '.flac'] else '.mp3'
            source_audio = self.chunk_dir / f"01{output_ext}"
            merged_audio = self.base_dir / f"{media_name}_audio.mp3"

            if source_audio.exists():
                shutil.copy(source_audio, merged_audio)
                print(f"  [成功] 复制音频到: {merged_audio.name}")
        else:
            # 视频模式：复制视频和音频文件
            source_video = self.chunk_dir / "01.mp4"
            source_audio = self.chunk_dir / "01.mp3"
            merged_video = self.base_dir / f"{media_name}_video.mp4"
            merged_audio = self.base_dir / f"{media_name}_audio.mp3"

            if source_video.exists():
                shutil.copy(source_video, merged_video)
                print(f"  [成功] 复制视频到: {merged_video.name}")

            if source_audio.exists():
                shutil.copy(source_audio, merged_audio)
                print(f"  [成功] 复制音频到: {merged_audio.name}")

    def generate_shadowing_audio(self):
        """生成跟读练习音频（纯音频模式）"""
        import json

        repeat_count = self.shadowing_params.get('repeat_count', 3)
        pause_between_repeats = self.shadowing_params.get('pause_between_repeats', True)
        pause_mode = self.shadowing_params.get('pause_mode', 'multiplier')
        pause_multiplier = self.shadowing_params.get('pause_multiplier', 1.5)
        pause_seconds = self.shadowing_params.get('pause_seconds', 2.0)

        # 初始化时长缓存（如果不存在）
        if not hasattr(self, 'video_durations_cache'):
            self.video_durations_cache = {}

        print(f"  从{'视频' if not self.is_audio_only else '音频'}片段重新提取WAV格式音频...")

        # 跟读模式：从视频/音频片段重新提取WAV格式音频，（避免MP3编码器延迟）
        output_ext = '.wav'
        total = len(self.segments)
        source_ext = '.mp3' if self.is_audio_only else '.mp4'

        for i, seg in enumerate(self.segments, 1):
            seg_source = self.chunk_dir / f"{seg['filename']}{source_ext}"
            seg_audio_wav = self.chunk_dir / f"{seg['filename']}{output_ext}"

            # 检查源文件是否存在
            if not seg_source.exists():
                print(f"    [{i}/{total}] 跳过 {seg['filename']} (源文件不存在)")
                continue

            # 获取或测量时长
            duration = self.video_durations_cache.get(seg['index'])
            if duration is None:
                # 测量源文件时长
                probe_result = subprocess.run([
                    'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'json',
                    str(seg_source)
                ], capture_output=True, text=True, encoding='utf-8', errors='ignore')

                if probe_result.returncode == 0:
                    duration = float(json.loads(probe_result.stdout)['format']['duration'])
                    self.video_durations_cache[seg['index']] = duration
                else:
                    print(f"    [{i}/{total}] 跳过 {seg['filename']} (无法测量时长)")
                    continue

            print(f"    [{i}/{total}] 提取 {seg['filename']}.wav")

            # 提取WAV格式音频（精确时长，无编码器延迟）
            cmd = [
                'ffmpeg', '-y',
                '-i', str(seg_source),
                '-vn',
                '-t', str(duration),
                '-acodec', 'pcm_s16le',
                '-ar', '48000',
                str(seg_audio_wav)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            if result.returncode != 0:
                print(f"    [错误] WAV音频提取失败: {seg['filename']}")
                if result.stderr:
                    print(f"    [错误] {result.stderr[:200]}")
                raise Exception(f"WAV音频提取失败: {seg['filename']}")

        print(f"  生成跟读练习静音片段...")

        # 记录实际时长（毫秒精度，用于字幕生成）
        theoretical_durations_ms = []
        silence_files = {}

        # 为每个音频片段生成一个静音片段（只生成一次）
        for seg in self.segments:
            # 测量实际生成的WAV音频文件时长（而不是视频时长）
            seg_audio_wav = self.chunk_dir / f"{seg['filename']}{output_ext}"

            probe_result = subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(seg_audio_wav)
            ], capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if probe_result.returncode != 0:
                print(f"    [错误] 无法测量音频文件时长: {seg_audio_wav}")
                continue

            seg_duration_actual = float(json.loads(probe_result.stdout)['format']['duration'])
            seg_duration_actual_ms = int(seg_duration_actual * 1000)

            # 计算停顿时长
            if pause_mode == 'multiplier':
                pause_duration_ms = int(seg_duration_actual_ms * pause_multiplier)
            else:
                pause_duration_ms = int(pause_seconds * 1000)

            pause_duration_sec = pause_duration_ms / 1000.0

            # 生成静音文件（WAV格式，无编码器延迟）
            silence_file = self.chunk_dir / f"silence_{seg['index']}.wav"
            result = subprocess.run([
                'ffmpeg', '-y', '-f', 'lavfi',
                '-i', 'anullsrc=r=48000:cl=stereo',
                '-t', str(pause_duration_sec),
                '-acodec', 'pcm_s16le',
                '-ar', '48000',
                str(silence_file)
            ], capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if result.returncode != 0:
                print(f"    [错误] 静音文件生成失败: silence_{seg['index']}.wav")
                if result.stderr:
                    print(f"    [错误] {result.stderr[:200]}")
                raise Exception(f"静音文件生成失败: silence_{seg['index']}.wav")

            silence_files[seg['index']] = silence_file

            # 测量实际静音时长
            probe_result = subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(silence_file)
            ], capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if probe_result.returncode != 0:
                print(f"    [错误] 无法测量静音文件时长: {silence_file}")
                raise Exception(f"无法测量静音文件时长: {silence_file}")

            silence_duration_actual = float(json.loads(probe_result.stdout)['format']['duration'])

            silence_duration_actual_ms = int(silence_duration_actual * 1000)

            # 记录实际时长（用于字幕生成）
            for i in range(repeat_count):
                theoretical_durations_ms.append(('audio', seg_duration_actual_ms, seg['text']))
                if pause_between_repeats or i == repeat_count - 1:
                    silence_text = seg['text'] if self.show_pause_subtitles else ''
                    theoretical_durations_ms.append(('silence', silence_duration_actual_ms, silence_text))

        # 分两步合并：先为每个片段生成重复版本，再合并所有片段（避免命令行过长）
        print(f"  正在生成每个音频片段的重复版本...")
        repeated_audio_files = []

        for seg in self.segments:
            seg_audio = self.chunk_dir / f"{seg['filename']}{output_ext}"
            silence_file = silence_files[seg['index']]
            repeated_audio = self.chunk_dir / f"{seg['filename']}_repeated.wav"

            # 构建该片段的重复序列
            input_files = []
            concat_inputs = []

            for i in range(repeat_count):
                input_files.extend(['-i', str(seg_audio)])
                concat_inputs.append(f"[{len(input_files)//2-1}:a]")

                if pause_between_repeats or i == repeat_count - 1:
                    input_files.extend(['-i', str(silence_file)])
                    concat_inputs.append(f"[{len(input_files)//2-1}:a]")

            filter_str = ''.join(concat_inputs) + f"concat=n={len(concat_inputs)}:v=0:a=1[outa]"

            cmd = ['ffmpeg', '-y'] + input_files + [
                '-filter_complex', filter_str,
                '-map', '[outa]',
                '-acodec', 'pcm_s16le',
                '-ar', '48000',
                str(repeated_audio)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if result.returncode != 0:
                print(f"    [错误] 音频片段重复失败: {seg['filename']}")
                if result.stderr:
                    print(f"    [错误] {result.stderr[:200]}")
                raise Exception(f"音频片段重复失败: {seg['filename']}")

            repeated_audio_files.append(repeated_audio)

        # 使用concat demuxer合并所有重复后的音频片段（避免命令行过长）
        print(f"  正在合并所有音频片段...")
        media_name = Path(self.video_path).stem
        output_audio = self.base_dir / f"{media_name}_shadowing_audio.mp3"

        # 创建concat列表文件
        concat_list = self.chunk_dir / "concat_audio_list.txt"
        with open(concat_list, 'w', encoding='utf-8') as f:
            for repeated_audio in repeated_audio_files:
                f.write(f"file '{repeated_audio.name}'\n")

        # 使用concat demuxer合并，但需要重新编码为MP3
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(concat_list),
            '-c:a', 'libmp3lame',
            '-b:a', '192k',
            str(output_audio)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            print(f"    [错误] 音频合并失败")
            if result.stderr:
                print(f"    [错误] {result.stderr[:500]}")
            raise Exception("音频合并失败")

        # 保存实际时长信息供字幕生成使用
        self.shadowing_audio_durations_ms = theoretical_durations_ms

        print(f"  [成功] 跟读练习音频已生成: {output_audio.name}")

    def generate_shadowing_video(self):
        """生成跟读练习视频（视频模式）- 简化版本"""
        import shutil
        import json

        repeat_count = self.shadowing_params.get('repeat_count', 3)
        pause_between_repeats = self.shadowing_params.get('pause_between_repeats', True)
        pause_mode = self.shadowing_params.get('pause_mode', 'multiplier')
        pause_multiplier = self.shadowing_params.get('pause_multiplier', 1.5)
        pause_seconds = self.shadowing_params.get('pause_seconds', 2.0)

        print(f"  生成跟读练习停顿片段...")

        # 记录理论时长（用于生成字幕，使用毫秒保证精度）
        theoretical_durations_ms = []
        # 记录实际时长（用于调试对比）
        actual_durations_debug = []

        # 为每个字幕片段生成一个停顿片段（只生成一次）
        pause_files = {}
        seg_durations_ms = {}  # 保存每个片段的实际时长
        pause_durations_ms = {}  # 保存每个停顿的实际时长

        # 检测第一个片段的音频参数（用于生成匹配的停顿文件）
        first_seg_video = self.chunk_dir / f"{self.segments[0]['filename']}.mp4"
        audio_params = self._detect_audio_params(str(first_seg_video))
        print(f"  检测到音频参数: 采样率={audio_params['sample_rate']}Hz, 声道={audio_params['channels']}")

        # 对于多声道音频（如5.1环绕声），降混为立体声以避免静音片段噪音
        if audio_params['channels'] > 2:
            print(f"  检测到多声道音频({audio_params['channels']}声道)，将降混为立体声以确保静音质量")
            audio_params['channels'] = 2
            audio_params['channel_layout'] = 'stereo'

        # 并行生成停顿文件（视频编码占用大量内存）
        max_workers = calculate_optimal_workers('video')
        print(f"  使用 {max_workers} 个并行worker生成停顿文件")

        def generate_pause_file(seg):
            """生成单个片段的停顿文件"""
            seg_video = self.chunk_dir / f"{seg['filename']}.mp4"
            duration_ms = seg['end_time'] - seg['start_time']
            duration_sec = duration_ms / 1000.0

            if pause_mode == 'multiplier':
                pause_duration_ms = int(duration_ms * pause_multiplier)
                pause_duration_sec = pause_duration_ms / 1000.0
            else:
                pause_duration_sec = pause_seconds
                pause_duration_ms = int(pause_seconds * 1000)

            print(f"    片段{seg['index']}: 理论时长={duration_ms}ms, 停顿={pause_duration_ms}ms")

            # 提取最后一帧
            last_frame = self.chunk_dir / f"frame_{seg['index']}.jpg"
            subprocess.run([
                'ffmpeg', '-y',
                '-sseof', '-0.1',
                '-i', str(seg_video),
                '-vframes', '1',
                '-q:v', '2',
                str(last_frame)
            ], capture_output=True)

            # 生成停顿片段
            pause_file = self.chunk_dir / f"pause_{seg['index']}.mp4"
            anullsrc_params = f"anullsrc=r={audio_params['sample_rate']}:cl={audio_params['channel_layout']}"

            subprocess.run([
                'ffmpeg', '-y',
                '-loop', '1', '-framerate', '25',
                '-i', str(last_frame),
                '-f', 'lavfi', '-i', anullsrc_params,
                '-t', str(pause_duration_sec),
                '-c:v', 'libx264',
                '-r', '25',
                '-preset', self.preset,
                '-crf', str(self.crf),
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k',
                '-ar', str(audio_params['sample_rate']),
                str(pause_file)
            ], capture_output=True)

            # 测量实际时长
            seg_duration_actual = float(json.loads(subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(seg_video)
            ], capture_output=True, text=True).stdout)['format']['duration'])

            pause_duration_actual = float(json.loads(subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(pause_file)
            ], capture_output=True, text=True).stdout)['format']['duration'])

            return {
                'index': seg['index'],
                'pause_file': pause_file,
                'seg_duration_ms': int(seg_duration_actual * 1000),
                'pause_duration_ms': int(pause_duration_actual * 1000),
                'text': seg['text'],
                'duration_sec': duration_sec,
                'seg_duration_actual': seg_duration_actual,
                'pause_duration_sec': pause_duration_sec,
                'pause_duration_actual': pause_duration_actual
            }

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [(executor.submit(generate_pause_file, seg), seg) for seg in self.segments]
            # 按提交顺序等待，确保结果顺序正确
            for future, seg in futures:
                try:
                    result = future.result()
                    pause_files[result['index']] = result['pause_file']
                    seg_durations_ms[result['index']] = result['seg_duration_ms']
                    pause_durations_ms[result['index']] = result['pause_duration_ms']

                    for i in range(repeat_count):
                        theoretical_durations_ms.append(('video', result['seg_duration_ms'], result['text']))
                        if pause_between_repeats or i == repeat_count - 1:
                            pause_text = result['text'] if self.show_pause_subtitles else ''
                            theoretical_durations_ms.append(('pause', result['pause_duration_ms'], pause_text))

                    actual_durations_debug.append({
                        'index': result['index'],
                        'theoretical_video': result['duration_sec'],
                        'actual_video': result['seg_duration_actual'],
                        'theoretical_pause': result['pause_duration_sec'],
                        'actual_pause': result['pause_duration_actual']
                    })
                except Exception as e:
                    print(f"    [错误] 生成停顿文件失败 (片段{seg['index']}): {e}")

        # 分两步合并：先为每个片段生成重复版本，再合并所有片段（避免命令行过长）
        print(f"  正在生成每个片段的重复版本...")
        # 重复文件生成使用stream copy，较轻量（使用stream copy避免二次编码）
        repeat_workers = calculate_optimal_workers('light')
        print(f"  使用 {repeat_workers} 个并行worker生成重复文件")

        def generate_repeated_file(seg):
            """生成单个片段的重复文件"""
            seg_video = self.chunk_dir / f"{seg['filename']}.mp4"
            pause_file = pause_files[seg['index']]
            repeated_file = self.chunk_dir / f"{seg['filename']}_repeated.mp4"

            repeat_list = self.chunk_dir / f"repeat_list_{seg['index']}.txt"
            with open(repeat_list, 'w', encoding='utf-8') as f:
                for i in range(repeat_count):
                    f.write(f"file '{seg_video.name}'\n")
                    if pause_between_repeats or i == repeat_count - 1:
                        f.write(f"file '{pause_file.name}'\n")

            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(repeat_list),
                '-c', 'copy',
                str(repeated_file)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if result.returncode != 0:
                print(f"    [错误] 片段重复失败: {seg['filename']}")
                if result.stderr:
                    print(f"    [错误] {result.stderr[:200]}")
                raise Exception(f"片段重复失败: {seg['filename']}")

            repeat_list.unlink()
            return repeated_file

        repeated_files = []
        with ThreadPoolExecutor(max_workers=repeat_workers) as executor:
            futures = [(executor.submit(generate_repeated_file, seg), seg) for seg in self.segments]
            # 按提交顺序等待，确保文件顺序正确
            for future, seg in futures:
                try:
                    repeated_file = future.result()
                    repeated_files.append(repeated_file)
                except Exception as e:
                    print(f"    [错误] 生成重复文件失败 (片段{seg['index']}): {e}")
                    raise

        # 测量所有repeated文件的实际时长，用于生成精确的字幕
        print(f"  正在测量repeated文件实际时长...")
        repeated_durations_ms = []
        for repeated_file in repeated_files:
            probe_result = subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(repeated_file)
            ], capture_output=True, text=True)

            if probe_result.returncode == 0:
                duration_actual = float(json.loads(probe_result.stdout)['format']['duration'])
                repeated_durations_ms.append(int(duration_actual * 1000))
            else:
                # 如果测量失败，使用理论时长
                repeated_durations_ms.append(0)

        # 重新生成字幕数据（使用实际测量的时长）
        theoretical_durations_ms = []
        for seg_idx, seg in enumerate(self.segments):
            # 直接使用已测量的单个片段时长（更精确）
            seg_duration_ms = seg_durations_ms[seg['index']]
            pause_duration_ms = pause_durations_ms[seg['index']]

            # 根据 pause_between_repeats 的值构建字幕列表
            if pause_between_repeats:
                # 启用重复间停顿: 每次播放后都停顿
                for i in range(repeat_count):
                    theoretical_durations_ms.append(('video', seg_duration_ms, seg['text']))
                    pause_text = seg['text'] if self.show_pause_subtitles else ''
                    theoretical_durations_ms.append(('pause', pause_duration_ms, pause_text))
            else:
                # 禁用重复间停顿: 连续播放N次，最后才停顿
                for i in range(repeat_count):
                    theoretical_durations_ms.append(('video', seg_duration_ms, seg['text']))
                # 最后添加一次停顿
                pause_text = seg['text'] if self.show_pause_subtitles else ''
                theoretical_durations_ms.append(('pause', pause_duration_ms, pause_text))

        # 使用concat demuxer合并所有重复后的片段（使用stream copy，快速且节省内存）
        print(f"  正在合并所有片段...")
        media_name = Path(self.video_path).stem
        output_video = self.base_dir / f"{media_name}_shadowing_video.mp4"

        # 创建concat列表文件
        concat_list = self.chunk_dir / "concat_repeated_list.txt"
        with open(concat_list, 'w', encoding='utf-8') as f:
            for repeated_file in repeated_files:
                f.write(f"file '{repeated_file.name}'\n")

        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(concat_list),
            '-c', 'copy',
            str(output_video)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            print(f"    [错误] 视频合并失败")
            if result.stderr:
                print(f"    [错误详情] {result.stderr[-2000:]}")
            if result.stdout:
                print(f"    [输出] {result.stdout[-1000:]}")
            raise Exception("视频合并失败")

        # 保存理论时长信息供字幕生成使用（毫秒精度）
        self.shadowing_theoretical_durations_ms = theoretical_durations_ms

        # 将时长信息写入调试文件（写入脚本所在目录）
        if getattr(sys, 'frozen', False):
            script_dir = Path(sys.executable).parent
        else:
            script_dir = Path(__file__).parent

        debug_file = script_dir / "shadowing_debug.txt"
        with open(debug_file, 'w', encoding='utf-8') as f:
            f.write("跟读模式调试信息\n")
            f.write("=" * 60 + "\n")
            f.write(f"生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"输出目录: {self.base_dir}\n")
            f.write("=" * 60 + "\n\n")

            # 写入片段对比信息
            f.write("片段时长对比（理论 vs 实际）:\n")
            f.write("-" * 60 + "\n")
            for seg_info in actual_durations_debug:
                f.write(f"片段 {seg_info['index']}:\n")
                f.write(f"  视频: 理论={seg_info['theoretical_video']:.3f}s, 实际={seg_info['actual_video']:.3f}s, ")
                f.write(f"差异={abs(seg_info['theoretical_video'] - seg_info['actual_video']):.3f}s\n")
                f.write(f"  停顿: 理论={seg_info['theoretical_pause']:.3f}s, 实际={seg_info['actual_pause']:.3f}s, ")
                f.write(f"差异={abs(seg_info['theoretical_pause'] - seg_info['actual_pause']):.3f}s\n\n")

            # 写入理论时长序列（毫秒精度）
            f.write("\n理论时长序列（用于字幕生成，毫秒精度）:\n")
            f.write("-" * 60 + "\n")
            cumulative_time_ms = 0
            for i, (seg_type, duration_ms, text) in enumerate(theoretical_durations_ms, 1):
                if seg_type == 'video':
                    f.write(f"{i}. 视频片段: {duration_ms}ms (累计: {cumulative_time_ms}ms - {cumulative_time_ms + duration_ms}ms)\n")
                    f.write(f"   字幕: {text}\n\n")
                else:
                    f.write(f"{i}. 停顿: {duration_ms}ms (累计: {cumulative_time_ms}ms - {cumulative_time_ms + duration_ms}ms)\n\n")
                cumulative_time_ms += duration_ms

            f.write("\n" + "=" * 60 + "\n")
            f.write(f"总片段数: {len(theoretical_durations_ms)}\n")
            video_count = sum(1 for t, _, _ in theoretical_durations_ms if t == 'video')
            pause_count = sum(1 for t, _, _ in theoretical_durations_ms if t == 'pause')
            f.write(f"视频片段: {video_count}个\n")
            f.write(f"停顿片段: {pause_count}个\n")
            total_theoretical_ms = sum(d for _, d, _ in theoretical_durations_ms)
            f.write(f"理论总时长: {total_theoretical_ms}ms ({total_theoretical_ms/1000:.3f}秒)\n")

            # 获取最终视频的实际时长
            try:
                probe_result = subprocess.run([
                    'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'json',
                    str(output_video)
                ], capture_output=True, text=True)
                probe_data = json.loads(probe_result.stdout)
                actual_video_duration_sec = float(probe_data['format']['duration'])
                actual_video_duration_ms = int(actual_video_duration_sec * 1000)
                f.write(f"\n实际视频时长: {actual_video_duration_ms}ms ({actual_video_duration_sec:.3f}秒)\n")
                diff_ms = abs(actual_video_duration_ms - total_theoretical_ms)
                f.write(f"时长差异: {diff_ms}ms ({diff_ms/1000:.3f}秒)\n")
                if diff_ms > 100:  # 差异超过100ms
                    f.write(f"\n⚠️ 警告：时长差异超过100ms！\n")
                    f.write(f"   理论时长: {total_theoretical_ms}ms\n")
                    f.write(f"   实际时长: {actual_video_duration_ms}ms\n")
                    f.write(f"   差异: {diff_ms}ms\n")
                else:
                    f.write(f"\n✓ 时长匹配良好（差异 < 100ms）\n")
            except Exception as e:
                f.write(f"无法获取实际视频时长: {e}\n")

        print(f"  [调试] 调试信息已保存到: {debug_file}")

        print(f"  [成功] 跟读练习视频已生成: {output_video.name}")

    def generate_shadowing_subtitles(self):
        """生成跟读练习字幕（视频和音频）"""
        media_name = Path(self.video_path).stem

        # 生成视频字幕
        if hasattr(self, 'shadowing_theoretical_durations_ms') and self.shadowing_theoretical_durations_ms:
            video_file = self.base_dir / f"{media_name}_shadowing_video.mp4"
            srt_file = self.base_dir / f"{media_name}_shadowing_video.srt"
            print(f"  正在生成跟读练习视频字幕（毫秒精度）...")
            self._generate_subtitle_file(srt_file, self.shadowing_theoretical_durations_ms, 'video', video_file)
            print(f"  [成功] 跟读练习视频字幕已生成: {srt_file.name}")

        # 生成音频字幕
        if hasattr(self, 'shadowing_audio_durations_ms') and self.shadowing_audio_durations_ms:
            audio_file = self.base_dir / f"{media_name}_shadowing_audio.mp3"
            srt_file = self.base_dir / f"{media_name}_shadowing_audio.srt"
            print(f"  正在生成跟读练习音频字幕（毫秒精度）...")
            self._generate_subtitle_file(srt_file, self.shadowing_audio_durations_ms, 'audio', audio_file)
            print(f"  [成功] 跟读练习音频字幕已生成: {srt_file.name}")

    def _generate_subtitle_file(self, srt_file, durations_ms, media_type, media_file=None):
        """生成字幕文件的辅助函数"""
        # 使用毫秒精度的理论时长信息
        if durations_ms:
            # 计算理论总时长
            theoretical_duration_ms = sum(duration for _, duration, _ in durations_ms)

            # 测量实际媒体文件时长
            scale_factor = 1.0
            if media_file and media_file.exists():
                try:
                    probe_result = subprocess.run([
                        'ffprobe', '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'json',
                        str(media_file)
                    ], capture_output=True, text=True)

                    if probe_result.returncode == 0:
                        actual_duration_sec = float(json.loads(probe_result.stdout)['format']['duration'])
                        actual_duration_ms = int(actual_duration_sec * 1000)

                        # 计算缩放比例
                        scale_factor = actual_duration_ms / theoretical_duration_ms

                        print(f"    理论时长: {theoretical_duration_ms}ms, 实际时长: {actual_duration_ms}ms, 缩放比例: {scale_factor:.6f}")
                except Exception as e:
                    print(f"    [警告] 无法测量实际时长，使用理论时长: {e}")

            current_time_ms = 0
            srt_index = 1

            with open(srt_file, 'w', encoding='utf-8') as f:
                for seg_type, duration_ms, text in durations_ms:
                    # 检查是视频/音频片段还是停顿/静音片段
                    if seg_type in ['video', 'audio', 'pause', 'silence']:
                        start_ms = int(current_time_ms * scale_factor)
                        end_ms = int((current_time_ms + duration_ms) * scale_factor)

                        # 如果是停顿/静音且文本为空，跳过写入字幕（但仍累加时间）
                        if seg_type in ['pause', 'silence'] and not text:
                            current_time_ms += duration_ms
                            continue

                        # 媒体片段或有文本的停顿片段，写入字幕
                        f.write(f"{srt_index}\n")
                        f.write(f"{self.ms_to_srt(start_ms)} --> {self.ms_to_srt(end_ms)}\n")
                        f.write(text.replace('\\N', '\n') + "\n\n")

                        srt_index += 1
                        current_time_ms += duration_ms
                    else:
                        # 其他类型片段，跳过时间
                        current_time_ms += duration_ms

    def _extract_leading_number(self, file_path):
        """提取文件名前面的数字用于排序（照搬外部脚本逻辑）"""
        import re
        filename = os.path.basename(str(file_path))
        m = re.match(r"(\d+)", os.path.splitext(filename)[0])
        return int(m.group(1)) if m else 0

    def _escape_ffmpeg_path(self, file_path, base_dir=None):
        """转义FFmpeg concat协议的特殊字符，确保100%兼容所有语言

        处理的问题：
        1. 单引号（法语等）：' → '\''  例如：C'est → C'\''est
        2. Windows反斜杠：\ → /  (FFmpeg更推荐正斜杠)
        3. 长路径+中文：使用相对路径避免Windows路径问题

        支持的语言字符：
        - 西班牙语：á é í ó ú ñ ü ¿ ¡
        - 法语：à â é è ê ë ï î ô ù û ü ÿ ç œ æ « »
        - 德语：ä ö ü Ä Ö Ü ß
        - 菲律宾语：á é í ó ú ñ
        - 泰语：ก ข ค ง จ ฉ ช ซ ฌ ญ
        - 越南语：à á ả ã ạ ă ằ ắ ẳ ẵ ặ
        - 中文/日语/韩语/俄语/阿拉伯语等所有Unicode字符
        """
        # 如果提供了base_dir，使用相对路径（解决Windows长路径+中文问题）
        if base_dir:
            try:
                file_path = os.path.relpath(file_path, base_dir)
            except ValueError:
                # 如果无法计算相对路径（不同驱动器），使用绝对路径
                file_path = os.path.abspath(file_path)

        path_str = str(file_path)

        # 1. 转义单引号：' → '\''
        # 这是FFmpeg concat协议的标准转义方式
        # 原理：先结束当前引号，插入转义的单引号，再开始新引号
        escaped = path_str.replace("'", "'\\''")

        # 2. 统一路径分隔符：反斜杠 → 正斜杠
        # FFmpeg在所有平台都支持正斜杠，避免转义问题
        if os.name == 'nt':  # Windows系统
            escaped = escaped.replace('\\', '/')

        return escaped

    def ms_to_srt(self, ms):
        """毫秒转SRT时间格式 HH:MM:SS,mmm"""
        # 四舍五入到整数毫秒，避免浮点数精度问题
        total_ms = round(ms)

        hours = total_ms // 3600000
        minutes = (total_ms % 3600000) // 60000
        secs = (total_ms % 60000) // 1000
        millis = total_ms % 1000

        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def _detect_audio_params(self, video_file):
        """检测视频文件的音频参数（采样率、声道数、声道布局）"""
        try:
            cmd = [
                'ffprobe', '-v', 'error',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=sample_rate,channels,channel_layout',
                '-of', 'json',
                video_file
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if result.returncode == 0:
                data = json.loads(result.stdout)
                if 'streams' in data and len(data['streams']) > 0:
                    stream = data['streams'][0]
                    sample_rate = int(stream.get('sample_rate', 48000))
                    channels = int(stream.get('channels', 2))
                    channel_layout = stream.get('channel_layout', 'stereo')

                    # 标准化声道布局名称
                    if channels == 1:
                        channel_layout = 'mono'
                    elif channels == 2:
                        channel_layout = 'stereo'

                    return {
                        'sample_rate': sample_rate,
                        'channels': channels,
                        'channel_layout': channel_layout
                    }
        except Exception as e:
            print(f"  [警告] 检测音频参数失败: {e}")

        # 默认返回标准参数
        return {
            'sample_rate': 48000,
            'channels': 2,
            'channel_layout': 'stereo'
        }


def main():
    try:
        print("=" * 60)
        print("按字幕切割音视频软件 v1.0.0")
        print("=" * 60 + "\n")
        sys.stdout.flush()

        parser = argparse.ArgumentParser(description='完整导出处理器')
        parser.add_argument('--config', required=True, help='配置JSON文件路径')
        args = parser.parse_args()

        if not os.path.exists(args.config):
            print(f"错误: 配置文件不存在: {args.config}")
            sys.stdout.flush()
            return 1

        exporter = CompleteExporter(args.config)
        result = exporter.run()

        # 调试信息
        print(f"\n[DEBUG] exporter.run() returned: {result}")
        sys.stdout.flush()

        return result

    except Exception as e:
        print(f"\n[FATAL] main() exception: {e}")
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.stdout.flush()

    # 检查是否需要手动关闭CMD窗口
    try:
        # 查找 --config 参数后的配置文件路径
        config_file = None
        for i, arg in enumerate(sys.argv):
            if arg == '--config' and i + 1 < len(sys.argv):
                config_file = sys.argv[i + 1]
                break

        if config_file and os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 检查主配置或 shadowing 配置中的 keep_cmd_open
                keep_cmd_open = config.get('keep_cmd_open', False)
                if not keep_cmd_open:
                    shadowing_config = config.get('shadowing', {})
                    keep_cmd_open = shadowing_config.get('keep_cmd_open', False)
                if keep_cmd_open:
                    input("\n按回车键退出...")
    except:
        pass

    sys.exit(exit_code)
