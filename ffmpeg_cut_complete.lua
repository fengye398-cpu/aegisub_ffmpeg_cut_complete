script_name = "导出选中字幕行对应的音频视频"
script_description = "一键导出视频、音频、字幕片段并自动合并，生成精确时间轴的完整字幕文件"
script_author = ""
script_version = "1.0.0"

-- ========== 工具函数 ==========

-- 获取脚本所在目录
function get_script_dir()
    local info = debug.getinfo(1, "S")
    local script_path = info.source:match("@?(.*)")
    local dir = script_path:match("^(.*)[/\\][^/\\]+$")
    if dir then
        return dir .. "\\"
    else
        return "..\\"
    end
end

-- 毫秒转HH:MM:SS格式
function ms_to_time_str(ms)
    local seconds = ms / 1000
    local hours = math.floor(seconds / 3600)
    local minutes = math.floor((seconds % 3600) / 60)
    local secs = seconds % 60
    return string.format("%02d:%02d:%06.3f", hours, minutes, secs)
end

-- 毫秒转SRT时间格式 (HH:MM:SS,mmm)
function ms_to_srt_time(ms)
    local total_seconds = math.floor(ms / 1000)
    local milliseconds = ms % 1000
    local hours = math.floor(total_seconds / 3600)
    local minutes = math.floor((total_seconds % 3600) / 60)
    local seconds = total_seconds % 60
    return string.format("%02d:%02d:%02d,%03d", hours, minutes, seconds, milliseconds)
end

-- 清理文件名中的非法字符
function sanitize_filename(text)
    local cleaned = text:gsub('[<>:"/\\|?*]', '_')
    cleaned = cleaned:gsub('[\n\r]', ' ')
    cleaned = cleaned:gsub('%s+', ' ')
    cleaned = cleaned:gsub('^%s+', ''):gsub('%s+$', '')
    if cleaned == "" then
        cleaned = "clip"
    end
    return cleaned
end

-- 检测并过滤过短片段（<= 200ms）
function detect_and_filter_short_segments(segments)
    local MIN_DURATION = 200  -- 最小时长阈值200ms，如需修改请改此值
    local filtered_segments = {}
    local short_segments_details = {}
    local short_indices = {}

    for i, seg in ipairs(segments) do
        if seg.duration <= MIN_DURATION then
            -- 使用SRT序号(如果有)或者index
            local display_number = seg.srt_number or seg.index

            -- 记录过短片段详细信息
            -- 生成SRT格式的时间轴字符串
            local time_range = ms_to_srt_time(seg.start_time) .. " --> " .. ms_to_srt_time(seg.end_time)

            table.insert(short_segments_details, {
                original_index = seg.index,
                srt_number = display_number,
                duration = seg.duration,
                start_time = seg.start_time,
                end_time = seg.end_time,
                time_range = time_range,
                text = seg.text
            })
            table.insert(short_indices, display_number)
        else
            -- 保留片段（保持原序号）
            table.insert(filtered_segments, seg)
        end
    end

    -- 构建过滤信息
    local short_info = {
        detected = (#short_segments_details > 0),
        total_count = #segments,
        short_count = #short_segments_details,
        remaining_count = #filtered_segments,
        short_indices = short_indices,
        details = short_segments_details
    }

    return filtered_segments, short_info
end

-- 在Aegisub中显示过短片段信息
function print_short_segments_info(short_info)
    if not short_info.detected then
        aegisub.debug.out("============================================================\n")
        aegisub.debug.out("【过短片段检测】\n")
        aegisub.debug.out("============================================================\n")
        aegisub.debug.out(string.format("总片段数: %d 个\n", short_info.total_count))
        aegisub.debug.out("检测结果: 未检测到过短片段\n")
        aegisub.debug.out("所有片段均符合要求\n")
        aegisub.debug.out("============================================================\n\n")
        return
    end

    -- 有过短片段，显示详细信息
    aegisub.debug.out("============================================================\n")
    aegisub.debug.out("【过短片段自动过滤】\n")
    aegisub.debug.out("============================================================\n")
    aegisub.debug.out(string.format("总片段数: %d 个\n", short_info.total_count))
    aegisub.debug.out(string.format("过短片段(<= 200ms): %d 个 (%.1f%%)\n",
        short_info.short_count,
        short_info.short_count / short_info.total_count * 100))
    aegisub.debug.out("已自动过滤，不会被切割\n")
    aegisub.debug.out("\n")

    -- 显示序号列表（最多前20个）
    local indices_str = ""
    for i = 1, math.min(20, #short_info.short_indices) do
        if i > 1 then
            indices_str = indices_str .. ", "
        end
        indices_str = indices_str .. tostring(short_info.short_indices[i])
    end
    if #short_info.short_indices > 20 then
        indices_str = indices_str .. ", ..."
    end
    aegisub.debug.out("过滤的片段序号(SRT序号): " .. indices_str .. "\n")
    aegisub.debug.out("\n")
    aegisub.debug.out(string.format("剩余片段数: %d 个\n", short_info.remaining_count))
    aegisub.debug.out("============================================================\n\n")
end

-- 导出JSON格式数据
function export_json(filepath, data)
    local f = io.open(filepath, "w")
    if not f then
        aegisub.debug.out("ERROR: 无法创建配置文件: " .. filepath .. "\n")
        return false
    end

    -- 简单的JSON序列化
    local function escape_string(s)
        s = s:gsub('\\', '\\\\')
        s = s:gsub('"', '\\"')
        s = s:gsub('\n', '\\n')
        s = s:gsub('\r', '\\r')
        s = s:gsub('\t', '\\t')
        return s
    end

    f:write("{\n")
    f:write('  "video_path": "' .. escape_string(data.video_path:gsub("\\", "/")) .. '",\n')
    f:write('  "mode": "' .. data.mode .. '",\n')
    f:write('  "naming": "' .. data.naming .. '",\n')
    f:write('  "crf": ' .. data.crf .. ',\n')
    f:write('  "preset": "' .. data.preset .. '",\n')
    f:write('  "gap": ' .. data.gap .. ',\n')
    f:write('  "keep_cmd_open": ' .. tostring(data.keep_cmd_open) .. ',\n')
    f:write('  "segments": [\n')

    for i, seg in ipairs(data.segments) do
        f:write('    {\n')
        f:write('      "index": ' .. seg.index .. ',\n')
        f:write('      "filename": "' .. escape_string(seg.filename) .. '",\n')
        f:write('      "text": "' .. escape_string(seg.text) .. '",\n')
        f:write('      "start_time": ' .. seg.start_time .. ',\n')
        f:write('      "end_time": ' .. seg.end_time .. ',\n')
        f:write('      "duration": ' .. seg.duration .. '\n')
        if i < #data.segments then
            f:write('    },\n')
        else
            f:write('    }\n')
        end
    end

    f:write('  ]')

    -- 添加 short_segments_info（如果存在）
    if data.short_segments_info then
        f:write(',\n')
        f:write('  "short_segments_info": {\n')
        f:write('    "detected": ' .. tostring(data.short_segments_info.detected) .. ',\n')
        f:write('    "total_count": ' .. data.short_segments_info.total_count .. ',\n')
        f:write('    "short_count": ' .. data.short_segments_info.short_count .. ',\n')
        f:write('    "remaining_count": ' .. data.short_segments_info.remaining_count .. ',\n')
        f:write('    "short_indices": [')
        for i, idx in ipairs(data.short_segments_info.short_indices) do
            f:write(tostring(idx))
            if i < #data.short_segments_info.short_indices then
                f:write(', ')
            end
        end
        f:write('],\n')

        -- 添加 details 数组
        f:write('    "details": [\n')
        for i, detail in ipairs(data.short_segments_info.details) do
            f:write('      {\n')
            f:write('        "srt_number": ' .. detail.srt_number .. ',\n')
            f:write('        "duration": ' .. detail.duration .. ',\n')
            f:write('        "time_range": "' .. escape_string(detail.time_range) .. '"\n')
            f:write('      }')
            if i < #data.short_segments_info.details then
                f:write(',')
            end
            f:write('\n')
        end
        f:write('    ]\n')

        f:write('  }')
    end

    -- 添加 shadowing 参数（如果存在）
    if data.shadowing then
        f:write(',\n')
        f:write('  \"shadowing\": {\n')
        f:write('    \"repeat_count\": ' .. data.shadowing.repeat_count .. ',\n')
        f:write('    \"pause_between_repeats\": ' .. tostring(data.shadowing.pause_between_repeats) .. ',\n')
        f:write('    \"pause_mode\": \"' .. data.shadowing.pause_mode .. '\",\n')
        f:write('    \"pause_multiplier\": ' .. data.shadowing.pause_multiplier .. ',\n')
        f:write('    \"pause_seconds\": ' .. data.shadowing.pause_seconds .. ',\n')
        f:write('    \"show_pause_subtitles\": ' .. tostring(data.shadowing.show_pause_subtitles) .. ',\n')
        f:write('    \"keep_cmd_open\": ' .. tostring(data.shadowing.keep_cmd_open) .. '\n')
        f:write('  }\n')
    else
        f:write('\n')
    end

    f:write('}\n')
    f:close()
    return true
end

-- ========== 主导出函数 ==========

function export_complete(subs, sel, mode, naming, custom_crf, custom_preset, keep_cmd_open, custom_segments, shadowing_params)
    -- 获取媒体文件路径（支持视频和音频）
    local props = aegisub.project_properties()
    local media_path = ""

    -- 优先检查video_file
    if props.video_file and props.video_file ~= "" then
        media_path = props.video_file
    -- 如果video_file为空，检查audio_file
    elseif props.audio_file and props.audio_file ~= "" then
        media_path = props.audio_file
    end

    -- 验证媒体文件
    if media_path == "" then
        aegisub.debug.out("ERROR: 未加载视频/音频文件\n")
        return
    end

    -- 检测媒体类型
    local file_ext = media_path:match("%.([^%.]+)$")
    local audio_exts = {mp3=true, wav=true, flac=true, aac=true, m4a=true, ogg=true, wma=true}
    local is_audio = audio_exts[file_ext:lower()] or false

    -- 判断是否连续模式
    local is_continuous = (mode == "continuous")
    local is_shadowing = (mode == "shadowing")

    aegisub.debug.out("========== 开始切割 ==========\n")
    aegisub.debug.out((is_audio and "音频文件: " or "视频文件: ") .. media_path .. "\n")

    if is_continuous then
        aegisub.debug.out("导出模式: 连续切割\n")
        aegisub.debug.out("时间范围: 第一条字幕到最后一条字幕\n")
    elseif is_shadowing then
        aegisub.debug.out("选中片段数: " .. #sel .. "\n")
        aegisub.debug.out("导出模式: 跟读练习\n")
        aegisub.debug.out("命名方式: " .. (naming == "index" and "按序号" or "按序号+字幕") .. "\n")
        aegisub.debug.out("播放次数: " .. shadowing_params.repeat_count .. "次\n")
        aegisub.debug.out("重复间停顿: " .. (shadowing_params.pause_between_repeats and "启用" or "禁用") .. "\n")
        if shadowing_params.pause_mode == "multiplier" then
            aegisub.debug.out("停顿时长: 句子时长 × " .. shadowing_params.pause_multiplier .. "倍\n")
        else
            aegisub.debug.out("停顿时长: " .. shadowing_params.pause_seconds .. "秒\n")
        end
        aegisub.debug.out("停顿时显示字幕: " .. (shadowing_params.show_pause_subtitles and "启用" or "禁用") .. "\n")
        aegisub.debug.out("CRF质量: " .. shadowing_params.crf .. "\n")
        aegisub.debug.out("编码预设: " .. shadowing_params.preset .. "\n")
    else
        aegisub.debug.out("选中片段数: " .. #sel .. "\n")
        aegisub.debug.out("导出模式: " .. (mode == "fast" and "快速编码" or "重新编码") .. "\n")
        aegisub.debug.out("命名方式: " .. (naming == "index" and "按序号" or "按序号+字幕") .. "\n")
        if mode == "reencode" then
            aegisub.debug.out("CRF质量: " .. custom_crf .. "\n")
            aegisub.debug.out("编码预设: " .. custom_preset .. "\n")
        end
    end

    aegisub.debug.out("=====================================\n\n")

    -- 后续使用media_path替代video_path
    local video_path = media_path

    -- 收集字幕数据：如果提供了custom_segments则使用，否则从sel收集
    local segments = {}

    if custom_segments then
        -- 使用自定义segments（连续模式）
        segments = custom_segments
        aegisub.debug.out("使用自定义字幕数据: " .. #segments .. " 条字幕\n\n")
    else
        -- 从选中项收集segments（片段模式）
        -- 计算SRT序号:遍历所有对话行并统计序号
        local srt_number_map = {}  -- 映射: 字幕文件行号 -> SRT序号
        local srt_counter = 0
        for line_idx = 1, #subs do
            if subs[line_idx].class == "dialogue" then
                srt_counter = srt_counter + 1
                srt_number_map[line_idx] = srt_counter
            end
        end

        for idx, i in ipairs(sel) do
            local line = subs[i]
            local filename_base

            if naming == "index" then
                filename_base = string.format("%02d", idx)
            else  -- naming == "index_text"
                local text_clean = line.text:gsub("\\N", " ")
                text_clean = sanitize_filename(text_clean)
                filename_base = string.format("%02d.%s", idx, text_clean)
            end

            table.insert(segments, {
                index = idx,
                srt_number = srt_number_map[i],  -- 添加SRT序号
                filename = filename_base,
                text = line.text,
                start_time = line.start_time,
                end_time = line.end_time,
                duration = line.end_time - line.start_time
            })
        end
    end

    -- 检测并过滤过短片段
    aegisub.debug.out("\n正在检测过短片段...\n")
    local filtered_segments, short_info = detect_and_filter_short_segments(segments)

    -- 显示过滤信息（在Aegisub中）
    print_short_segments_info(short_info)

    -- 检查是否有剩余片段
    if #filtered_segments == 0 then
        aegisub.debug.out("============================================================\n")
        aegisub.debug.out("【错误】过滤后没有剩余片段！\n")
        aegisub.debug.out("============================================================\n")
        aegisub.debug.out(string.format("原始片段数: %d 个\n", short_info.total_count))
        aegisub.debug.out(string.format("过短片段数: %d 个 (100%%)\n", short_info.short_count))
        aegisub.debug.out("所有片段时长均 <= 200毫秒，无法进行切割\n\n")
        aegisub.debug.out("建议:\n")
        aegisub.debug.out("1. 检查字幕文件是否正确\n")
        aegisub.debug.out("2. 字幕时间轴是否有误\n")
        aegisub.debug.out("3. 是否需要重新制作字幕\n")
        aegisub.debug.out("============================================================\n")
        return
    end

    -- 使用过滤后的segments
    segments = filtered_segments

    -- 生成配置数据
    local config = {
        video_path = video_path,
        mode = mode,
        naming = naming,
        crf = custom_crf or 24,  -- 使用自定义值或默认值
        preset = custom_preset or "veryfast",  -- 使用自定义值或默认值
        gap = 200,
        segments = segments,
        short_segments_info = short_info,  -- 添加过滤信息
        keep_cmd_open = keep_cmd_open or false  -- 手动关闭CMD窗口选项
    }

    -- 添加跟读模式参数
    if shadowing_params then
        config.shadowing = {
            repeat_count = shadowing_params.repeat_count or 3,
            pause_between_repeats = shadowing_params.pause_between_repeats,
            pause_mode = shadowing_params.pause_mode or "multiplier",
            pause_multiplier = shadowing_params.pause_multiplier or 1.5,
            pause_seconds = shadowing_params.pause_seconds or 2.5,
            show_pause_subtitles = shadowing_params.show_pause_subtitles,
            keep_cmd_open = shadowing_params.keep_cmd_open
        }
    end

    -- 导出配置JSON（使用临时目录避免权限问题）
    local temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
    local config_file = temp_dir .. "\\export_config.json"

    aegisub.debug.out("生成配置文件: " .. config_file .. "\n")
    if not export_json(config_file, config) then
        aegisub.debug.out("ERROR: 配置文件生成失败\n")
        return
    end

    aegisub.debug.out("配置文件生成成功\n\n")

    -- 调用Python后端
    aegisub.debug.out("========== 调用Python后端处理 ==========\n")
    aegisub.debug.out("正在启动Python脚本...\n")
    aegisub.debug.out("请稍候，处理过程可能需要几分钟\n")
    aegisub.debug.out("=====================================\n\n")

    -- 构建Python命令（工具文件仍然在脚本目录）
    local script_dir = get_script_dir()
    local venv_python = script_dir .. ".venv\\Scripts\\python.exe"
    local python_script = script_dir .. "complete_exporter.py"
    local exe_tool = script_dir .. "complete_exporter.exe"

    -- 优先级：虚拟环境Python + 脚本 > exe > 系统Python + 脚本
    local python_exe

    -- 检查Python脚本是否存在
    local script_exists = io.open(python_script, "r")
    if script_exists then
        script_exists:close()
    end

    -- 检查虚拟环境Python
    local test_venv = io.open(venv_python, "r")
    if test_venv and script_exists then
        test_venv:close()
        python_exe = venv_python
        aegisub.debug.out("使用虚拟环境Python\n")
    else
        if test_venv then test_venv:close() end

        -- 检查exe是否存在
        local test_exe = io.open(exe_tool, "r")
        if test_exe then
            test_exe:close()
            python_exe = nil
            aegisub.debug.out("使用打包的exe工具\n")
        else
            -- 最后尝试系统Python
            if script_exists then
                python_exe = "python"
                aegisub.debug.out("使用系统Python（可能缺少依赖）\n")
            else
                aegisub.debug.out("\n✗ 错误: 找不到可用的执行工具\n")
                aegisub.debug.out("\n请确保以下任一文件存在：\n")
                aegisub.debug.out("  - complete_exporter.exe (推荐)\n")
                aegisub.debug.out("  - complete_exporter.py + Python环境\n")
                return
            end
        end
    end

    -- 构建命令（直接执行，不重定向，让用户看到FFmpeg进度）
    -- 使用cmd /c来确保命令正确执行
    local cmd
    if python_exe then
        cmd = string.format('cmd /c ""%s" "%s" --config "%s""', python_exe, python_script, config_file)
    else
        cmd = string.format('cmd /c ""%s" --config "%s""', exe_tool, config_file)
    end

    -- 记录开始时间
    local start_time = os.time()
    aegisub.debug.out("开始时间: " .. os.date("%Y-%m-%d %H:%M:%S", start_time) .. "\n\n")

    -- 执行脚本（同步等待）
    -- Lua 5.2+ 返回 (success, type, code)，Lua 5.1 返回 code
    local success, exit_type, exit_code = os.execute(cmd)

    -- 记录结束时间和计算耗时
    local end_time = os.time()
    local elapsed_seconds = os.difftime(end_time, start_time)
    local minutes = math.floor(elapsed_seconds / 60)
    local seconds = elapsed_seconds % 60

    -- 兼容不同Lua版本
    local result_ok = false
    if type(success) == "boolean" then
        -- Lua 5.2+
        result_ok = success and (exit_code == 0)
    elseif type(success) == "number" then
        -- Lua 5.1
        result_ok = (success == 0)
    end

    aegisub.debug.out("\n========== 处理完成 ==========\n")
    aegisub.debug.out("结束时间: " .. os.date("%Y-%m-%d %H:%M:%S", end_time) .. "\n")
    aegisub.debug.out(string.format("总耗时: %d分%d秒\n", minutes, seconds))
    aegisub.debug.out("=====================================\n\n")

    if result_ok then
        aegisub.debug.out("[成功] 导出成功！\n\n")

        -- 读取Python脚本写入的输出路径（优先临时目录，fallback到脚本目录）
        local temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
        local output_path_file_temp = temp_dir .. "\\last_output_path.txt"
        local output_path_file_script = script_dir .. "last_output_path.txt"

        local output_path = nil
        local output_path_file_used = nil

        -- 尝试从临时目录读取
        local path_f = io.open(output_path_file_temp, "r")
        if path_f then
            output_path = path_f:read("*all")
            path_f:close()
            output_path_file_used = output_path_file_temp
        else
            -- Fallback: 尝试从脚本目录读取（兼容旧版本）
            path_f = io.open(output_path_file_script, "r")
            if path_f then
                output_path = path_f:read("*all")
                path_f:close()
                output_path_file_used = output_path_file_script
            end
        end

        if output_path and output_path ~= "" then
            aegisub.debug.out("输出位置: " .. output_path .. "\n")

            -- 清理临时文件
            if output_path_file_used then
                os.remove(output_path_file_used)
            end
        else
            -- 如果读取失败,使用推断方式
            local video_dir = video_path:match("^(.*)[/\\][^/\\]+$")
            if video_dir then
                aegisub.debug.out("输出位置: " .. video_dir .. "\n")
                aegisub.debug.out("(查找名为 \"视频名_时间戳\" 的最新文件夹)\n")
            else
                aegisub.debug.out("请查看视频文件所在目录\n")
            end
        end

        -- 询问是否打开输出目录
        if output_path and output_path ~= "" then
            local dialog_config = {
                {class="label", label="导出成功！", x=0, y=0, width=2, height=1},
                {class="label", label="输出位置:", x=0, y=1, width=1, height=1},
                {class="label", label=output_path, x=1, y=1, width=3, height=1},
                {class="label", label="", x=0, y=2, width=2, height=1},
                {class="label", label="是否打开输出目录？", x=0, y=3, width=2, height=1}
            }

            local buttons = {"打开目录", "关闭"}
            local button_pressed = aegisub.dialog.display(dialog_config, buttons)

            if button_pressed == "打开目录" then
                -- 获取视频文件名（不含扩展名）
                local video_name = video_path:match("^.*[/\\]([^/\\]+)$")
                if video_name then
                    video_name = video_name:match("^(.+)%.[^%.]+$") or video_name
                end

                -- 尝试定位到合并后的视频文件
                local merged_video = output_path .. "\\" .. video_name .. "_video.mp4"

                -- 检查文件是否存在
                local test_file = io.open(merged_video, "r")
                if test_file then
                    test_file:close()
                    -- 打开资源管理器并选中合并后的视频
                    os.execute(string.format('explorer /select,"%s"', merged_video))
                    aegisub.debug.out("\n已打开目录并选中: " .. video_name .. "_video.mp4\n")
                else
                    -- 如果合并视频不存在，直接打开目录
                    os.execute(string.format('explorer "%s"', output_path))
                    aegisub.debug.out("\n已打开输出目录\n")
                end
            end
        end

        -- 清理所有临时文件（成功导出后）
        local temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
        local temp_files = {"export_config.json", "last_output_path.txt", "last_error.txt"}
        for _, filename in ipairs(temp_files) do
            local temp_file = temp_dir .. "\\" .. filename
            pcall(function() os.remove(temp_file) end)
        end
    else
        aegisub.debug.out("[失败] 导出失败\n\n")

        -- 读取Python写入的错误文件（优先临时目录，fallback到脚本目录）
        local temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
        local error_file_temp = temp_dir .. "\\last_error.txt"
        local error_file_script = script_dir .. "last_error.txt"

        local error_content = nil
        local error_file_used = nil

        -- 尝试从临时目录读取
        local error_f = io.open(error_file_temp, "r")
        if error_f then
            error_content = error_f:read("*all")
            error_f:close()
            error_file_used = error_file_temp
        else
            -- Fallback: 尝试从脚本目录读取（兼容旧版本）
            error_f = io.open(error_file_script, "r")
            if error_f then
                error_content = error_f:read("*all")
                error_f:close()
                error_file_used = error_file_script
            end
        end

        -- 显示详细的错误信息
        if error_content and error_content ~= "" then
            aegisub.debug.out("========== 错误详情 ==========\n")
            aegisub.debug.out(error_content)
            aegisub.debug.out("=====================================\n\n")

            -- 清理错误文件
            if error_file_used then
                os.remove(error_file_used)
            end
        else
            aegisub.debug.out("错误代码: success=" .. tostring(success) .. ", type=" .. tostring(exit_type) .. ", code=" .. tostring(exit_code) .. "\n")
            aegisub.debug.out("处理过程中发生错误，请查看cmd窗口的输出信息\n\n")
        end

        -- 清理所有临时文件（失败情况）
        local temp_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
        local temp_files = {"export_config.json", "last_output_path.txt", "last_error.txt"}
        for _, filename in ipairs(temp_files) do
            local temp_file = temp_dir .. "\\" .. filename
            pcall(function() os.remove(temp_file) end)
        end
    end
end

-- ========== 包装函数 ==========

function export_fast_index(subs, sel)
    export_complete(subs, sel, "fast", "index", nil, nil, false)
end

function export_fast_index_text(subs, sel)
    export_complete(subs, sel, "fast", "index_text", nil, nil, false)
end

function export_reencode_index(subs, sel)
    -- 弹出对话框让用户自定义编码参数
    local dialog_config = {
        {class="label", label="CRF质量参数 (0-51，越小质量越高):", x=0, y=0, width=2, height=1},
        {class="intedit", name="crf", value=24, min=0, max=51, x=0, y=1, width=1, height=1},
        {class="label", label="提示: 18-28为常用范围，默认24", x=1, y=1, width=1, height=1},

        {class="label", label="编码预设 (越慢质量越好):", x=0, y=2, width=2, height=1},
        {class="dropdown", name="preset", value="veryfast",
         items={"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"},
         x=0, y=3, width=1, height=1},
        {class="label", label="推荐: veryfast(默认)、medium、veryfast", x=1, y=3, width=1, height=1},

        {class="checkbox", name="keep_cmd_open", label="手动关闭CMD窗口", value=false, x=0, y=4, width=2, height=1}
    }

    local button, result = aegisub.dialog.display(dialog_config, {"确定", "取消"})
    if button == "取消" or not button then
        return  -- 用户取消，不执行导出
    end

    -- 使用用户选择的参数
    export_complete(subs, sel, "reencode", "index", result.crf, result.preset, result.keep_cmd_open)
end

function export_reencode_index_text(subs, sel)
    -- 弹出对话框让用户自定义编码参数
    local dialog_config = {
        {class="label", label="CRF质量参数 (0-51，越小质量越高):", x=0, y=0, width=2, height=1},
        {class="intedit", name="crf", value=24, min=0, max=51, x=0, y=1, width=1, height=1},
        {class="label", label="提示: 18-28为常用范围，默认24", x=1, y=1, width=1, height=1},

        {class="label", label="编码预设 (越慢质量越好):", x=0, y=2, width=2, height=1},
        {class="dropdown", name="preset", value="veryfast",
         items={"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"},
         x=0, y=3, width=1, height=1},
        {class="label", label="推荐: veryfast(默认)、medium、veryfast", x=1, y=3, width=1, height=1},

        {class="checkbox", name="keep_cmd_open", label="手动关闭CMD窗口", value=false, x=0, y=4, width=2, height=1}
    }

    local button, result = aegisub.dialog.display(dialog_config, {"确定", "取消"})
    if button == "取消" or not button then
        return  -- 用户取消，不执行导出
    end

    -- 使用用户选择的参数
    export_complete(subs, sel, "reencode", "index_text", result.crf, result.preset, result.keep_cmd_open)
end

function export_continuous(subs, sel)
    -- 验证选择
    if #sel == 0 then
        aegisub.debug.out("ERROR: 未选中任何字幕\n")
        return
    end

    -- 获取首尾字幕的时间范围
    local first_line = subs[sel[1]]
    local last_line = subs[sel[#sel]]
    local start_time = first_line.start_time
    local end_time = last_line.end_time

    -- 遍历所有字幕，收集时间范围内的字幕
    local all_segments = {}
    local collected_count = 0
    local srt_counter = 0  -- SRT序号计数器

    for i = 1, #subs do
        local line = subs[i]

        -- 只处理对话行
        if line.class == "dialogue" then
            srt_counter = srt_counter + 1  -- 每个对话行都计数

            -- 完全包含在时间范围内的字幕
            if line.start_time >= start_time and line.end_time <= end_time then
                collected_count = collected_count + 1

                -- 固定使用序号命名（连续模式不需要文本命名）
                local filename_base = string.format("%02d", collected_count)

                table.insert(all_segments, {
                    index = collected_count,
                    srt_number = srt_counter,  -- 添加SRT序号
                    filename = filename_base,
                    text = line.text,
                    start_time = line.start_time,
                    end_time = line.end_time,
                    duration = line.end_time - line.start_time
                })
            end
        end
    end

    -- 按start_time排序（确保字幕顺序正确）
    table.sort(all_segments, function(a, b)
        return a.start_time < b.start_time
    end)

    -- 重新分配index（排序后）
    for idx, seg in ipairs(all_segments) do
        seg.index = idx
        seg.filename = string.format("%02d", idx)
    end

    -- 验证：如果没有收集到字幕，给出错误提示
    if #all_segments == 0 then
        aegisub.debug.out("ERROR: 时间范围内没有找到任何字幕！\n")
        aegisub.debug.out("请确保选中的首尾字幕之间有其他字幕存在。\n")
        return
    end

    -- 弹出对话框让用户自定义编码参数
    local dialog_config = {
        {class="label", label="连续切割模式：从第一条字幕到最后一条字幕切割完整片段", x=0, y=0, width=2, height=1},
        {class="label", label="（包含中间所有内容，保持相对时间轴）", x=0, y=1, width=2, height=1},
        {class="label", label="", x=0, y=2, width=2, height=1},
        {class="label", label="收集到 " .. #all_segments .. " 条字幕", x=0, y=3, width=2, height=1},
        {class="label", label="", x=0, y=4, width=2, height=1},

        {class="label", label="CRF质量参数 (0-51，越小质量越高):", x=0, y=5, width=2, height=1},
        {class="intedit", name="crf", value=24, min=0, max=51, x=0, y=6, width=1, height=1},
        {class="label", label="提示: 18-28为常用范围，默认24", x=1, y=6, width=1, height=1},

        {class="label", label="编码预设 (越慢质量越好):", x=0, y=7, width=2, height=1},
        {class="dropdown", name="preset", value="veryfast",
         items={"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"},
         x=0, y=8, width=1, height=1},
        {class="label", label="推荐: veryfast(默认)、medium、veryfast", x=1, y=8, width=1, height=1},

        {class="checkbox", name="keep_cmd_open", label="手动关闭CMD窗口", value=false, x=0, y=9, width=2, height=1}
    }

    local button, result = aegisub.dialog.display(dialog_config, {"确定", "取消"})
    if button == "取消" or not button then
        return  -- 用户取消，不执行导出
    end

    -- 用户确认导出，输出调试信息
    aegisub.debug.out("========== 连续切割模式 - 字幕收集 ==========\n")
    aegisub.debug.out("时间范围: " .. ms_to_time_str(start_time) .. " --> " .. ms_to_time_str(end_time) .. "\n")
    aegisub.debug.out("正在收集时间范围内的所有字幕...\n")
    aegisub.debug.out("收集完成: 共 " .. #all_segments .. " 条字幕\n")
    aegisub.debug.out("=====================================\n\n")

    -- 使用用户选择的参数，mode设置为"continuous"，传递收集的字幕数据
    export_complete(subs, sel, "continuous", "index", result.crf, result.preset, result.keep_cmd_open, all_segments)
end

-- 验证函数
function validate_video(subs, sel)
    if #sel == 0 then
        return false
    end

    -- 检查媒体文件路径
    local props = aegisub.project_properties()
    local media_path = ""

    -- 优先检查video_file
    if props.video_file and props.video_file ~= "" then
        media_path = props.video_file
    -- 如果video_file为空，检查audio_file
    elseif props.audio_file and props.audio_file ~= "" then
        media_path = props.audio_file
    end

    -- 最终检查：是否有有效的媒体路径
    if media_path == "" then
        return false
    end

    return true
end

-- ========== 注册菜单 ==========

aegisub.register_macro(
    script_name .. "/1. 快速编码模式/1. 按序号命名",
    "快速编码模式：使用重新编码固定参数切割片段；CRF24、预设速度veryfast",
    export_fast_index,
    validate_video
)

aegisub.register_macro(
    script_name .. "/1. 快速编码模式/2. 按序号+字幕命名",
    "快速编码模式：使用重新编码按序号+字幕文本命名文件",
    export_fast_index_text,
    validate_video
)

aegisub.register_macro(
    script_name .. "/2. 重新编码模式/1. 按序号命名",
    "重新编码模式：精确切割，帧级精度，可自定义切割参数，默认值CRF=24、预设速度veryfast",
    export_reencode_index,
    validate_video
)

aegisub.register_macro(
    script_name .. "/2. 重新编码模式/2. 按序号+字幕命名",
    "重新编码模式：精确切割，使用序号+字幕文本命名文件",
    export_reencode_index_text,
    validate_video
)

aegisub.register_macro(
    script_name .. "/3. 连续切割模式",
    "连续切割：从第一条字幕到最后一条字幕切割完整片段，保留中间所有内容，保持相对时间轴",
    export_continuous,
    validate_video
)

function export_shadowing(subs, sel)
    -- 弹出对话框让用户自定义跟读练习参数
    local dialog_config = {
        {class="label", label="跟读练习模式：每个句子重复播放N次，句子间自动停顿", x=0, y=0, width=4, height=1},

        {class="label", label="播放次数:", x=0, y=1, width=1, height=1},
        {class="intedit", name="repeat_count", value=3, min=1, max=10, x=1, y=1, width=1, height=1},
        {class="label", label="推荐: 3次", x=2, y=1, width=2, height=1},

        {class="checkbox", name="pause_between_repeats", label="重复间停顿 (每次播放后都停顿)", value=true, x=0, y=2, width=4, height=1},

        {class="label", label="停顿模式:", x=0, y=3, width=1, height=1},
        {class="dropdown", name="pause_mode", value="multiplier", items={"multiplier", "fixed"}, x=1, y=3, width=1, height=1},
        {class="label", label="multiplier=按倍数, fixed=固定秒数", x=2, y=3, width=2, height=1},

        {class="label", label="按倍数:", x=0, y=4, width=1, height=1},
        {class="floatedit", name="pause_multiplier", value=1.5, min=0.1, max=10.0, x=1, y=4, width=1, height=1},
        {class="label", label="句子时长 × 倍数，推荐: 1.5倍", x=2, y=4, width=2, height=1},

        {class="label", label="固定秒数:", x=0, y=5, width=1, height=1},
        {class="floatedit", name="pause_seconds", value=2.5, min=0.1, max=30.0, x=1, y=5, width=1, height=1},
        {class="label", label="推荐: 2.5秒", x=2, y=5, width=2, height=1},

        {class="label", label="CRF质量:", x=0, y=6, width=1, height=1},
        {class="intedit", name="crf", value=24, min=0, max=51, x=1, y=6, width=1, height=1},
        {class="label", label="0-51，越小质量越高，推荐: 18-28，默认24", x=2, y=6, width=2, height=1},

        {class="label", label="编码预设:", x=0, y=7, width=1, height=1},
        {class="dropdown", name="preset", value="veryfast",
         items={"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"},
         x=1, y=7, width=1, height=1},
        {class="label", label="越慢质量越好，推荐: veryfast(默认)、medium、veryfast", x=2, y=7, width=2, height=1},

        {class="checkbox", name="show_pause_subtitles", label="停顿时显示字幕", value=true, x=0, y=8, width=2, height=1},
        {class="checkbox", name="keep_cmd_open", label="手动关闭CMD窗口", value=false, x=2, y=8, width=2, height=1}
    }

    local button, result = aegisub.dialog.display(dialog_config, {"确定", "取消"})
    if button == "取消" or not button then
        return
    end

    -- 使用用户选择的参数，mode设置为"shadowing"
    export_complete(subs, sel, "shadowing", "index", result.crf, result.preset, result.keep_cmd_open, nil, result)
end

aegisub.register_macro(
    script_name .. "/4. 跟读练习模式",
    "跟读练习：每个句子重复播放N次，自动添加停顿，适合语言学习跟读练习",
    export_shadowing,
    validate_video
)
