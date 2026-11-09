#Requires -Version 5.1
<#
    USB 诊断与修复
    按 USB 接口协议特征（Class_FF & SubClass_42 & Prot_01）识别 ADB 接口，
    不依赖厂商 VID，换任何 Android 手机都适用。

    三种故障自动处理：
      1) 接口状态正常但未注册 ADB 设备接口 GUID  -> 补注册并重启接口
      2) 接口问题码 31/10（驱动无法加载）        -> 删除残留 GUID 并重启接口
      3) 问题码 43（端口复位失败）               -> 不写注册表，提示换线换口

    注意：情况 2 清掉残留后重新枚举出来的设备是「问题码 0 但未注册 GUID」，
    只修一遍不够，所以这里跑两轮，第一轮清残留、第二轮补注册，一次点完。
#>
param(
    [string]$LogPath = "$PSScriptRoot\..\usb-diag.log"
)

$ErrorActionPreference = 'SilentlyContinue'
$AdbGuid    = '{f72fe0d4-cbcb-407d-8814-9ed673d0dd6b}'   # ADB 设备接口类
$WinUsbGuid = '{dee824ef-729b-4a0e-9c14-b7117d33a817}'   # GUID_DEVINTERFACE_WINUSB

if (Test-Path $LogPath) { Remove-Item $LogPath -Force }

function Write-Both {
    param([string]$Message = '')
    Write-Host $Message
    $Message | Out-File -FilePath $LogPath -Append -Encoding utf8
}

# ---- 前置闸门：adb 已经能用就绝不碰注册表 ----
# 本工具的写入动作只针对「系统认到了接口但 adb 用不了」的情况。
# 若当前已有设备（device / unauthorized / offline），说明接口链路本身是通的，
# 改注册表只会把正常配置改坏（这是本项目踩过的真实坑，代价是问题码 31）。
$adbExe = Join-Path $PSScriptRoot '..\scrcpy\adb.exe'
if (Test-Path $adbExe) {
    $devLines = @(& $adbExe devices -l 2>$null) | Where-Object { $_ -match '^\S+\s+(device|unauthorized|offline)(\s|$)' }
    if ($devLines.Count -gt 0) {
        Write-Both ''
        Write-Both '当前 adb 已能看到设备，接口链路是通的，无需修改驱动配置：'
        foreach ($l in $devLines) { Write-Both ('  ' + $l.Trim()) }
        Write-Both ''
        if ($devLines -match 'unauthorized') {
            Write-Both '状态 unauthorized：请在手机屏幕上点「允许 USB 调试」并勾选「始终允许」。'
        }
        elseif ($devLines -match 'offline') {
            Write-Both '状态 offline：在命令行运行 adb kill-server 后重新插线即可。'
        }
        else {
            Write-Both '状态 device：设备已就绪，直接启动主程序即可投屏。'
        }
        Write-Both '如确实需要重装某台设备的驱动，请先拔掉所有手机再运行本工具。'
        exit 0
    }
}

function Get-AdbInterfaces {
    # 按兼容 ID 里的 SubClass_42 识别，这是所有 Android ADB 接口的通用特征
    $result = @()
    foreach ($d in (Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_*&MI_*' })) {
        $p = Get-PnpDeviceProperty -InstanceId $d.InstanceId
        $cid = ($p | Where-Object { $_.KeyName -eq 'DEVPKEY_Device_CompatibleIds' }).Data
        if ($cid -and (($cid -join ' ') -match 'SubClass_42')) {
            $result += [pscustomobject]@{
                Device  = $d
                Problem = ($p | Where-Object { $_.KeyName -eq 'DEVPKEY_Device_ProblemCode' }).Data
                VId     = (($d.InstanceId -split '\\')[1])
            }
        }
    }
    return ,$result
}

function Test-AdbGuidRegistered {
    param([string]$InstanceSuffix)
    $found = $false
    foreach ($cls in (Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceClasses')) {
        foreach ($sub in (Get-ChildItem $cls.PSPath)) {
            if ($sub.PSChildName -like "*$InstanceSuffix*" -and $sub.PSChildName -match 'f72fe0d4') {
                $found = $true
            }
        }
    }
    return $found
}

function Get-DeviceParamsKey {
    param([string]$InstanceId)
    $parts = $InstanceId -split '\\'          # USB, VID_xxx&PID_xxx&MI_xx, 6&xxxx&0&0002
    $inst = $parts[-1]
    return "HKLM:\SYSTEM\CurrentControlSet\Enum\$($parts[0])\$($parts[1])\$inst\Device Parameters"
}

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Both "管理员权限: $(if ($isAdmin) { '是' } else { '否（无法修改驱动配置，请右键以管理员身份运行）' })"

$touchedAny = $false

for ($pass = 1; $pass -le 2; $pass++) {
    $interfaces = @(Get-AdbInterfaces)

    if ($interfaces.Count -eq 0) {
        if ($pass -eq 1) {
            Write-Both ""
            Write-Both "未发现任何 ADB 接口。可能原因："
            Write-Both "  - 数据线只能供电、不能传数据"
            Write-Both "  - 手机未开启 USB 调试"
            Write-Both "  - 通知栏 USB 模式选的是「仅充电」"
            Write-Both "  - 接口枚举失败（设备管理器里显示为「未知 USB 设备」或问题码 43）"
            exit 0
        }
        break
    }

    Write-Both ""
    Write-Both "========== 第 $pass 轮 / 共 2 轮 =========="
    $touched = $false

    foreach ($item in $interfaces) {
        $id   = $item.Device.InstanceId
        $inst = ($id -split '\\')[-1]
        $key  = Get-DeviceParamsKey -InstanceId $id

        Write-Both ""
        Write-Both "接口: $id"
        Write-Both "  名称=$($item.Device.FriendlyName)  状态=$($item.Device.Status)  问题码=$($item.Problem)"
        Write-Both "  厂商段=$($item.VId)"

        if ($item.Problem -eq 0) {
            $hasGuid = Test-AdbGuidRegistered -InstanceSuffix $inst
            Write-Both "  ADB 接口注册: $(if ($hasGuid) { '已注册，正常' } else { '未注册 —— 这就是 adb 看不到设备的原因' })"

            if ($hasGuid) {
                Write-Both "  >> 无需修复。若 adb 仍为空，请看手机屏幕是否弹了「允许 USB 调试」。"
                continue
            }

            # 已经有单值 DeviceInterfaceGUID 且就是 ADB 接口类时，不要再加多值：
            # 两个值同时存在会让 winusb.sys 起不来（问题码 31）。
            $names = @()
            if (Test-Path $key) { $names = (Get-Item -Path $key).GetValueNames() }
            if ($names -contains 'DeviceInterfaceGUID') {
                $single = (Get-Item -Path $key).GetValue('DeviceInterfaceGUID')
                Write-Both "  >> 已有单值 DeviceInterfaceGUID = $single，不再写入多值（两个同时存在会冲突）"
                Write-Both "  >> 若 adb 仍看不到，请拔线重插一次让接口重新枚举"
                continue
            }

            if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
            New-ItemProperty -Path $key -Name 'DeviceInterfaceGUIDs' -PropertyType MultiString -Force `
                -Value @($WinUsbGuid, $AdbGuid) | Out-Null

            $written = (Get-Item -Path $key).GetValueNames() -contains 'DeviceInterfaceGUIDs'
            if (-not $written) {
                Write-Both "  >> 写入失败：需要管理员权限。请右键本文件 → 以管理员身份运行。"
                continue
            }
            Write-Both "  >> 已写入接口 GUID，正在重启该接口"
            Write-Both "  >> $((pnputil /remove-device $id 2>&1) -join ' ')"
            pnputil /scan-devices | Out-Null
            $touched = $true
        }
        elseif ($item.Problem -eq 31 -or $item.Problem -eq 10 -or $item.Problem -eq 28) {
            Write-Both "  驱动无法加载（问题码 $($item.Problem)）"
            $removed = $false
            if (Test-Path $key) {
                # 用值名判断，别用 Get-ItemProperty -Name：值不存在时它会返回整个键对象，
                # 于是每个候选值都被当成「删不掉」，日志里冒出一堆假失败。
                $names = (Get-Item -Path $key).GetValueNames()
                Write-Both "  当前已有的值: $($names -join ', ')"

                # 单值 DeviceInterfaceGUID 是设备自己上报的原始配置，必须留着；
                # 冲突来自后来加进去的多值 DeviceInterfaceGUIDs。只删多值即可恢复。
                if ($names -contains 'DeviceInterfaceGUIDs' -and $names -contains 'DeviceInterfaceGUID') {
                    Write-Both "  >> 检出单值/多值同时存在——这就是 winusb.sys 起不来的原因，删除多值保留原配置"
                    Remove-ItemProperty -Path $key -Name 'DeviceInterfaceGUIDs' -Force
                    if ((Get-Item -Path $key).GetValueNames() -contains 'DeviceInterfaceGUIDs') {
                        Write-Both "  >> 删除失败（需要管理员权限）"
                    }
                    else {
                        Write-Both "  >> 已删除 DeviceInterfaceGUIDs，保留 DeviceInterfaceGUID"
                        $removed = $true
                    }
                }
                elseif ($names -contains 'DeviceInterfaceGUIDs') {
                    Write-Both "  >> 只有多值，删除它恢复原始状态，下一轮再按需要重建"
                    Remove-ItemProperty -Path $key -Name 'DeviceInterfaceGUIDs' -Force
                    if ((Get-Item -Path $key).GetValueNames() -contains 'DeviceInterfaceGUIDs') {
                        Write-Both "  >> 删除失败（需要管理员权限）"
                    }
                    else {
                        Write-Both "  >> 已删除残留值"
                        $removed = $true
                    }
                }
                else {
                    Write-Both "  >> 注册表里没有残留 GUID，故障在驱动安装本身，下面重装接口"
                    $removed = $true
                }
            }
            Write-Both "  >> $((pnputil /remove-device $id 2>&1) -join ' ')"
            pnputil /scan-devices | Out-Null
            if ($removed -or $isAdmin) { $touched = $true }
        }
        elseif ($item.Problem -eq 43) {
            Write-Both "  端口复位失败（问题码 43）：设备复位后没有重新站起来，属于信号链路问题"
            Write-Both "  >> 这种情况改注册表无用，请换一根短数据线、直插机身 USB 口，不要经过扩展坞"
        }
        else {
            Write-Both "  异常状态，未自动处理。请到设备管理器查看该接口的详细信息。"
        }
    }

    $touchedAny = $touchedAny -or $touched
    if (-not $touched) { break }
    if ($pass -lt 2) {
        Write-Both ""
        Write-Both "等待 8 秒让 Windows 重新枚举设备 ..."
        Start-Sleep -Seconds 8
    }
}

Write-Both ""
Write-Both "修复流程结束（$(if ($touchedAny) { '已执行修改' } else { '未做修改' })）。"
