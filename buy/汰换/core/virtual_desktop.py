"""Windows 10/11 虚拟桌面操作（基于 comtypes，支持 Win11 24H2 build 26200）。

对外 API:
  create_desktop()          -> GUID  创建新虚拟桌面
  get_current_desktop_id()  -> GUID  取当前桌面 GUID
  move_window_to_desktop(hwnd, guid) -> bool  把窗口移到指定桌面

GUID 来源: MScholtes/VirtualDesktop 的 VirtualDesktop11-24H2.cs
（验证通过 Win11 build 26200）
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import comtypes
from comtypes import GUID, IUnknown, POINTER

comtypes.CoInitialize()

# ---------- GUID ----------
CLSID_ImmersiveShell = GUID("{C2F03A33-21F5-47FA-B4BB-156362A2F239}")
IID_IServiceProvider = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")

# 这个 CLSID 同时也是 QueryService 的 service ID
CLSID_VirtualDesktopManagerInternal = GUID("{C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B}")

CLSID_VirtualDesktopManager = GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
IID_IVirtualDesktopManager = GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")

# Win11 24H2 / 25H2 (build 26200+) 的正确 IID
IID_IVirtualDesktop = GUID("{3F07F4BE-B107-441A-AF0F-39D82529072C}")
IID_IVirtualDesktopManagerInternal = GUID("{53F5CA0B-158F-4124-900C-057158060B27}")


# ---------- 接口定义 ----------
class IServiceProvider(IUnknown):
    _iid_ = IID_IServiceProvider
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "QueryService",
                           [POINTER(GUID), POINTER(GUID), POINTER(ctypes.c_void_p)]),
    ]


class IVirtualDesktopManager(IUnknown):
    _iid_ = IID_IVirtualDesktopManager
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "IsWindowOnCurrentVirtualDesktop",
                           [wintypes.HWND, POINTER(wintypes.BOOL)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetWindowDesktopId",
                           [wintypes.HWND, POINTER(GUID)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "MoveWindowToDesktop",
                           [wintypes.HWND, GUID]),
    ]


class IVirtualDesktop(IUnknown):
    _iid_ = IID_IVirtualDesktop
    _methods_ = [
        # IsViewVisible(IApplicationView*, BOOL*)  -- 跳过，用占位
        comtypes.STDMETHOD(comtypes.HRESULT, "IsViewVisible",
                           [ctypes.c_void_p, POINTER(wintypes.BOOL)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetId", [POINTER(GUID)]),
    ]


class IVirtualDesktopManagerInternal(IUnknown):
    _iid_ = IID_IVirtualDesktopManagerInternal
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "GetCount", [POINTER(ctypes.c_int)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "MoveViewToDesktop",
                           [ctypes.c_void_p, ctypes.c_void_p]),
        comtypes.STDMETHOD(comtypes.HRESULT, "CanViewMoveDesktops",
                           [ctypes.c_void_p, POINTER(wintypes.BOOL)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetCurrentDesktop",
                           [POINTER(ctypes.c_void_p)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDesktops",
                           [POINTER(ctypes.c_void_p)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetAdjacentDesktop",
                           [ctypes.c_void_p, ctypes.c_int, POINTER(ctypes.c_void_p)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "SwitchDesktop", [ctypes.c_void_p]),
        comtypes.STDMETHOD(comtypes.HRESULT, "SwitchDesktopAndMoveForegroundView",
                           [ctypes.c_void_p]),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDesktop",
                           [POINTER(ctypes.c_void_p)]),
        comtypes.STDMETHOD(comtypes.HRESULT, "MoveDesktop",
                           [ctypes.c_void_p, ctypes.c_int]),
        comtypes.STDMETHOD(comtypes.HRESULT, "RemoveDesktop",
                           [ctypes.c_void_p, ctypes.c_void_p]),
        comtypes.STDMETHOD(comtypes.HRESULT, "FindDesktop",
                           [POINTER(GUID), POINTER(ctypes.c_void_p)]),
    ]


# ---------- 对外 API ----------
def _get_internal():
    """通过 ImmersiveShell.QueryService 拿 IVirtualDesktopManagerInternal。"""
    sp = comtypes.CoCreateInstance(
        CLSID_ImmersiveShell, interface=IServiceProvider,
        clsctx=comtypes.CLSCTX_ALL)
    ptr = ctypes.c_void_p()
    hr = sp.QueryService(
        ctypes.byref(CLSID_VirtualDesktopManagerInternal),
        ctypes.byref(IID_IVirtualDesktopManagerInternal),
        ctypes.byref(ptr))
    if hr != 0 or not ptr:
        raise OSError(
            f"QueryService 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return comtypes.cast(ptr, POINTER(IVirtualDesktopManagerInternal))


def create_desktop() -> GUID:
    """创建一个新虚拟桌面，返回其 GUID。"""
    internal = _get_internal()
    try:
        pp_vd = ctypes.c_void_p()
        hr = internal.CreateDesktop(ctypes.byref(pp_vd))
        if hr != 0 or not pp_vd:
            raise OSError(f"CreateDesktop 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        vd = comtypes.cast(pp_vd, POINTER(IVirtualDesktop))
        guid = GUID()
        hr = vd.GetId(ctypes.byref(guid))
        vd.Release()
        if hr != 0:
            raise OSError(f"IVirtualDesktop.GetId 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return guid
    finally:
        internal.Release()


def get_current_desktop_id() -> GUID:
    """取当前活动桌面的 GUID。"""
    vdm = comtypes.CoCreateInstance(
        CLSID_VirtualDesktopManager, interface=IVirtualDesktopManager,
        clsctx=comtypes.CLSCTX_ALL)
    try:
        user32 = ctypes.WinDLL("user32")
        hwnd = user32.GetForegroundWindow() or user32.GetDesktopWindow()
        guid = GUID()
        hr = vdm.GetWindowDesktopId(hwnd, ctypes.byref(guid))
        if hr != 0:
            raise OSError(
                f"GetWindowDesktopId 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return guid
    finally:
        vdm.Release()


def move_window_to_desktop(hwnd: int, desktop_guid: GUID) -> bool:
    """把窗口 hwnd 移到指定桌面。成功返回 True。"""
    vdm = comtypes.CoCreateInstance(
        CLSID_VirtualDesktopManager, interface=IVirtualDesktopManager,
        clsctx=comtypes.CLSCTX_ALL)
    try:
        hr = vdm.MoveWindowToDesktop(hwnd, desktop_guid)
        return hr == 0
    finally:
        vdm.Release()


def remove_desktop(desktop_guid: GUID) -> bool:
    """删除指定虚拟桌面（把上面的窗口合并到当前桌面）。成功返回 True。"""
    internal = _get_internal()
    try:
        # 1. 找到要删除的桌面 IVirtualDesktop*
        p_target = ctypes.c_void_p()
        hr = internal.FindDesktop(ctypes.byref(desktop_guid),
                                  ctypes.byref(p_target))
        if hr != 0 or not p_target:
            return False
        # 2. 取当前桌面作为 fallback
        p_fallback = ctypes.c_void_p()
        hr = internal.GetCurrentDesktop(ctypes.byref(p_fallback))
        if hr != 0 or not p_fallback:
            comtypes.cast(p_target, POINTER(IVirtualDesktop)).Release()
            return False
        # 3. 删除
        hr = internal.RemoveDesktop(p_target, p_fallback)
        comtypes.cast(p_target, POINTER(IVirtualDesktop)).Release()
        comtypes.cast(p_fallback, POINTER(IVirtualDesktop)).Release()
        return hr == 0
    finally:
        internal.Release()


if __name__ == "__main__":
    print("创建虚拟桌面...")
    g = create_desktop()
    print(f"新桌面 GUID: {g}")
    cur = get_current_desktop_id()
    print(f"当前桌面 GUID: {cur}")
    print("删除测试桌面...")
    print("删除结果:", remove_desktop(g))
    print("OK")
