"""借阅管理域视图。

从 views.py 拆出的独立业务域。URL 配置仍通过 views.py 门面 re-export 引用，
URL 名称与行为完全不变。
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string

from accounts.decorators import perm_required

from .forms import BorrowForm
from .models import Archive, BorrowRecord
from .services.common import _is_ajax, _log


@login_required
@perm_required("archive", "read")
def borrow_list(request):
    status = request.GET.get("status")
    qs = BorrowRecord.objects.select_related("archive")
    if status:
        # 刷新逾期状态
        for r in qs.filter(return_time__isnull=True):
            r.refresh_status()
            r.save(update_fields=["status"])
        qs = qs.filter(status=status)
    return render(request, "archive/borrow_list.html", {"records": qs, "status": status})


@login_required
@perm_required("archive", "create")
def borrow_create(request, archive_pk):
    arc = get_object_or_404(Archive, pk=archive_pk)
    if arc.is_borrowed:
        messages.error(request, f"档案 {arc.code} 已借出，无法再次借阅")
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": f"档案 {arc.code} 已借出，无法再次借阅"}, status=422)
        return redirect("archive:archive_detail", pk=arc.pk)
    form = BorrowForm(request.POST or None)
    if form.is_valid():
        rec = form.save(commit=False)
        rec.archive = arc
        rec.save()
        _log(request, "借阅", f"档案#{arc.id}", f"借出档案: {arc.code} 给 {rec.borrower}，应还 {rec.due_time}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "借阅登记成功"})
        return redirect("archive:archive_detail", pk=arc.pk)
    if _is_ajax(request):
        html = render_to_string(
            "archive/_borrow_form_modal.html", {"form": form, "archive": arc}, request=request
        )
        # 打开弹窗（GET）返回 ok:true + html；提交校验失败（POST）返回 422 重新注入表单
        if request.method == "POST":
            return JsonResponse({"ok": False, "html": html}, status=422)
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/borrow_form.html", {"form": form, "archive": arc})


@login_required
@perm_required("archive", "update")
def borrow_return(request, pk):
    rec = get_object_or_404(BorrowRecord, pk=pk)
    if request.method == "POST":
        from django.utils import timezone
        rec.return_time = timezone.now()
        rec.refresh_status()
        rec.save()
        _log(request, "归还", f"档案#{rec.archive_id}", f"归还档案: {rec.archive.code}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "归还成功"})
        return redirect("archive:borrow_list")
    return render(request, "archive/borrow_confirm_return.html", {"record": rec})
