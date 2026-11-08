"""lagdoctor/winapi/webview2.py's vtable slots and IIDs, pinned against Microsoft's WebView2.idl.

The app calls WebView2 through raw vtable slots: one off calls another method with the wrong arguments. The IDL is the
SDK's own (Microsoft.Web.WebView2 1.0.4314.35), fetched and checked against the tests' pin by tests/downloads.py, and
read by tests/webview2_idl.py. That reader is proven twice, so a pass is not a reader agreeing with itself: on a literal
IDL here, without the download, and against numbers counted from a different file, WebView2.h's C vtables: the
spike's tables (scripts/webview_spike.py, which matched that header with 0 mismatches) and the slots and IIDs M1 adds.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from tests import webview2_idl as idl
from tests.webview2_idl import Interface

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import webview_spike as ws  # noqa: E402

APP_MODULE = "lagdoctor.winapi.webview2"

# Read from WebView2.h 1.0.4314.35's C vtables and MIDL_INTERFACE lines (the SDK's other file), never from the IDL:
# what M1 calls beyond the spike.
HEADER_SLOTS = {
    ("ICoreWebView2", "ExecuteScript"): 29,
    ("ICoreWebView2_17", "PostSharedBufferToScript"): 116,
    ("ICoreWebView2Environment12", "CreateSharedBuffer"): 24,
    ("ICoreWebView2SharedBuffer", "get_Size"): 3,
    ("ICoreWebView2SharedBuffer", "get_Buffer"): 4,
    ("ICoreWebView2SharedBuffer", "Close"): 7,
}
HEADER_IIDS = {
    "ICoreWebView2_17": "702E75D4-FD44-434D-9D70-1A68A6B1192A",
    "ICoreWebView2Environment12": "F503DB9B-739F-48DD-B151-FDFCF253F54E",
    "ICoreWebView2SharedBuffer": "B747A495-0C6F-449E-97B8-2F81E9D6AB43",
    "ICoreWebView2ExecuteScriptCompletedHandler": "49511172-CC67-4BCA-9923-137112F4C4CC",
}


# ---------------------------------------------------------------------------------------- the reader, offline --

SNIPPET = r'''
// A comment that names interface Fake : IUnknown { HRESULT Gone(); }
import "objidl.idl";
cpp_quote("// interface Quoted : IUnknown { HRESULT Gone(); }")

[uuid(11111111-2222-3333-4444-555555555555), version(1.0)]
library Lib {

interface Child;        // a forward declaration: no body, no slots

[v1_enum]
typedef enum KIND { KIND_A, KIND_B = 0x2 } KIND;
typedef struct POINTS { INT32 x; INT32 y; } POINTS;

/// The parent. A doc comment's HRESULT Hidden([in] int x); is no method, and its interface is no interface.
[uuid(aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee), object,
 pointer_default(unique)]
interface Parent : IUnknown {
  /* interface Fake : IUnknown { HRESULT Gone(); } */
  [propget] HRESULT Value([out, retval] UINT32* value);
  [propput] HRESULT Value([in] UINT32 value);
  HRESULT Run(
      [in, size_is(count)] LPCWSTR* items,
      [in] UINT32 count);
}

[uuid(AAAAAAAA-0000-0000-0000-000000000001), object, pointer_default(unique)]
interface Child : Parent {
  HRESULT Stop([in] LPCWSTR why /* , [in] int ignored */);
  [propget] HRESULT Name([out, retval] LPWSTR* name);
}
}
'''


def test_the_reader_names_methods_in_vtable_order_after_every_parents():
    found = idl.parse(SNIPPET)
    assert found == [
        Interface("Parent", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "IUnknown", ("get_Value", "put_Value", "Run")),
        Interface("Child", "AAAAAAAA-0000-0000-0000-000000000001", "Parent", ("Stop", "get_Name")),
    ]
    assert idl.slots(idl.by_name(found), "Child") == {
        "QueryInterface": 0, "AddRef": 1, "Release": 2, "get_Value": 3, "put_Value": 4, "Run": 5, "Stop": 6,
        "get_Name": 7}


@pytest.mark.parametrize("text, why", [
    ("[uuid(aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)] interface A : IUnknown { HRESULT Run(); UINT32 count; }",
     "no method"),
    ("[uuid(aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)] interface A : IUnknown { HRESULT Run(); /* never closed }",
     r"a /\* on line 1 is never closed"),
    ("interface A : IUnknown { HRESULT Run(); }", "no attributes"),
])
def test_what_the_reader_cannot_read_stops_it(text, why):
    with pytest.raises(ValueError, match=why):
        idl.parse(text)


def test_an_interface_defined_twice_or_on_an_undefined_parent_stops_the_reader():
    one = "[uuid(aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)] interface A : {} {{ HRESULT Run(); }}\n"
    with pytest.raises(ValueError, match="defined twice"):
        idl.by_name(idl.parse(one.format("IUnknown") * 2))
    with pytest.raises(ValueError, match="B is not defined"):
        idl.slots(idl.by_name(idl.parse(one.format("B"))), "A")


def test_iids_are_the_same_whatever_their_case_and_only_when_their_digits_are():
    assert idl.same_iid("76eceacb-0462-4d94-ac83-423a6793775e", "76ECEACB-0462-4D94-AC83-423A6793775E")
    assert not idl.same_iid("76eceacb-0462-4d94-ac83-423a6793775e", "76eceacb-0462-4d94-ac83-423a6793775f")
    assert not idl.same_iid("{76eceacb-0462-4d94-ac83-423a6793775e}", "{76eceacb-0462-4d94-ac83-423a6793775e}")
    assert not idl.same_iid("", "")


# ------------------------------------------------------------------------------------ the pinned WebView2.idl --

@pytest.fixture(scope="module")
def sdk(webview2_idl) -> tuple[list[Interface], dict[str, Interface]]:
    found = idl.parse(webview2_idl.read_text(encoding="utf-8"))
    return found, idl.by_name(found)


def test_the_pinned_idl_defines_297_interfaces_each_once_each_with_its_own_iid_and_a_whole_vtable(sdk):
    found, defs = sdk
    assert len(found) == len(defs) == 297                   # WebView2.h has 297 too (docs/research/desktop-app.md)
    assert len({itf.iid.upper() for itf in found}) == 297
    for name in defs:                                       # each one's parents reach IUnknown, no name twice
        assert len(idl.slots(defs, name)) >= 3, name


def test_the_reader_gives_every_slot_and_iid_counted_from_webview2_h(sdk):
    _, defs = sdk
    compared = 0
    for iface, methods in ws.SLOTS.items():
        got = idl.slots(defs, iface)
        for method, slot in methods.items():
            assert got.get(method) == slot, (iface, method, got.get(method), slot)
            compared += 1
    for (iface, method), slot in HEADER_SLOTS.items():
        assert idl.slots(defs, iface).get(method) == slot, (iface, method)
        compared += 1
    iids = 0
    for iface, iid in [*ws.IIDS.items(), *HEADER_IIDS.items()]:
        if iface == idl.IUNKNOWN:                             # imported by the IDL, not defined in it
            assert idl.same_iid(iid, idl.IUNKNOWN_IID)
        else:
            assert idl.same_iid(defs[iface].iid, iid), (iface, defs[iface].iid, iid)
        iids += 1
    assert (compared, iids) == (111 + 6, 38 + 4)              # the spike's tables and the header's additions, all


def test_the_apps_module_names_only_slots_and_iids_the_idl_gives(sdk):
    found, defs = sdk
    app = importlib.import_module(APP_MODULE)
    names = [itf.name for itf in found]
    for iface in sorted((set(app.SLOTS) | set(app.IIDS)) - {idl.IUNKNOWN}):   # defined once: exactly one IID
        assert names.count(iface) == 1, iface
    for iface, methods in app.SLOTS.items():
        got = idl.slots(defs, iface)
        for method, slot in methods.items():
            assert got.get(method) == slot, (iface, method, got.get(method), slot)
    for iface, iid in app.IIDS.items():
        want = idl.IUNKNOWN_IID if iface == idl.IUNKNOWN else defs[iface].iid
        assert idl.same_iid(iid, want), (iface, iid, want)
    # What M1 adds to the spike is there, at the header's slots.
    assert {key: app.SLOTS[key[0]][key[1]] for key in HEADER_SLOTS} == HEADER_SLOTS
    assert set(HEADER_IIDS) <= set(app.IIDS)
