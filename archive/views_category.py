"""类别管理域视图。

从 views.py 拆出的独立业务域。URL 配置仍通过 views.py 门面 re-export 引用，
URL 名称与行为完全不变。
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import perm_required
from records.crud import _per_page

from .forms import CategoryForm
from .models import Category
from .services.common import _is_ajax, _log


@login_required
@perm_required("archive", "read")
def category_list(request):
    qs = Category.objects.all().order_by("sort_order", "id")
    # 后端分页（10/30/50）
    per_page = _per_page(request)
    page_obj = Paginator(qs, per_page).get_page(request.GET.get("page"))
    return render(request, "archive/category_list.html", {
        "categories": page_obj, "page_obj": page_obj, "per_page": per_page,
    })


@login_required
@perm_required("archive", "create")
def category_create(request):
    form = CategoryForm(request.POST or None)
    if form.is_valid():
        cat = form.save()
        _log(request, "新增", f"类别#{cat.id}", f"新增类别: {cat.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"类别 {cat.name} 已创建"})
        return redirect("archive:category_list")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/category_form.html", {"form": form, "mode": "create"})


@login_required
@perm_required("archive", "update")
def category_edit(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    form = CategoryForm(request.POST or None, instance=cat)
    if form.is_valid():
        cat = form.save()
        _log(request, "编辑", f"类别#{cat.id}", f"编辑类别: {cat.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "类别已更新"})
        return redirect("archive:category_list")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "edit", "category": cat}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "edit", "category": cat}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/category_form.html", {"form": form, "mode": "edit", "category": cat})


@login_required
@perm_required("archive", "delete")
def category_delete(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    if cat.archives.exists():
        messages.error(request, f"类别 {cat.name} 下还有档案，无法删除")
        return redirect("archive:category_list")
    if request.method == "POST":
        _log(request, "删除", f"类别#{cat.id}", f"删除类别: {cat.name}")
        cat.delete()
        return redirect("archive:category_list")
    return render(request, "archive/category_confirm_delete.html", {"category": cat})
