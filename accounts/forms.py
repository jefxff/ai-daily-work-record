"""认证相关表单：登录、注册、管理员建号/编辑/重置密码、角色权限。"""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.models import User


BOOTSTRAP_INPUT = {"class": "form-control"}
BOOTSTRAP_SELECT = {"class": "form-select"}


class LoginForm(forms.Form):
    # autocomplete 提示密码管理器正确填充，替代原先的 autocomplete="off" 反模式（不影响任何提交数据）
    username = forms.CharField(label="用户名", max_length=150, widget=forms.TextInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "请输入用户名", "autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(label="密码", widget=forms.PasswordInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "请输入密码", "autocomplete": "current-password"}))


class RegisterForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150, widget=forms.TextInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "登录用户名（不可更改）", "autocomplete": "username"}))
    password = forms.CharField(label="密码", widget=forms.PasswordInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "至少 8 位", "autocomplete": "new-password"}))
    confirm = forms.CharField(label="确认密码", widget=forms.PasswordInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "再次输入密码", "autocomplete": "new-password"}))
    real_name = forms.CharField(label="姓名", max_length=50, widget=forms.TextInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "真实姓名"}))
    department = forms.CharField(label="部门", max_length=100, required=False, widget=forms.TextInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "可选"}))
    position = forms.CharField(label="岗位", max_length=100, required=False, widget=forms.TextInput(
        attrs={**BOOTSTRAP_INPUT, "placeholder": "可选"}))

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("该用户名已被注册。")
        return username

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("confirm")
        if password and confirm and password != confirm:
            self.add_error("confirm", "两次输入的密码不一致。")
        if password:
            # 借用标准密码校验（最小长度/常见密码/相似度等）
            user = User(username=cleaned.get("username", ""))
            password_validation.validate_password(password, user)
        return cleaned


class AdminCreateUserForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150, widget=forms.TextInput(BOOTSTRAP_INPUT))
    password = forms.CharField(label="初始密码", widget=forms.PasswordInput(BOOTSTRAP_INPUT))
    real_name = forms.CharField(label="姓名", max_length=50, required=False, widget=forms.TextInput(BOOTSTRAP_INPUT))
    department = forms.CharField(label="部门", max_length=100, required=False, widget=forms.TextInput(BOOTSTRAP_INPUT))
    position = forms.CharField(label="岗位", max_length=100, required=False, widget=forms.TextInput(BOOTSTRAP_INPUT))
    is_active = forms.BooleanField(label="账号启用", required=False, initial=True,
                                   widget=forms.CheckboxInput({"class": "form-check-input"}))

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("该用户名已存在。")
        return username

    def clean_password(self):
        password = self.cleaned_data["password"]
        password_validation.validate_password(password)
        return password


class AdminEditUserForm(forms.Form):
    real_name = forms.CharField(label="姓名", max_length=50, required=False, widget=forms.TextInput(BOOTSTRAP_INPUT))
    department = forms.CharField(label="部门", max_length=100, required=False, widget=forms.TextInput(BOOTSTRAP_INPUT))
    position = forms.CharField(label="岗位", max_length=100, required=False, widget=forms.TextInput(BOOTSTRAP_INPUT))
    is_active = forms.BooleanField(label="账号启用", required=False,
                                   widget=forms.CheckboxInput({"class": "form-check-input"}))
    is_staff = forms.BooleanField(label="可登录 Admin 后台", required=False,
                                  widget=forms.CheckboxInput({"class": "form-check-input"}))


class AdminResetPasswordForm(forms.Form):
    new_password = forms.CharField(label="新密码", widget=forms.PasswordInput(BOOTSTRAP_INPUT))
    confirm = forms.CharField(label="确认新密码", widget=forms.PasswordInput(BOOTSTRAP_INPUT))

    def clean(self):
        cleaned = super().clean()
        pwd = cleaned.get("new_password")
        if pwd:
            password_validation.validate_password(pwd)
        if pwd and cleaned.get("confirm") != pwd:
            self.add_error("confirm", "两次输入的密码不一致。")
        return cleaned


class RoleForm(forms.Form):
    code = forms.CharField(label="角色编码", max_length=50, widget=forms.TextInput(BOOTSTRAP_INPUT),
                           help_text="仅字母/数字/下划线，保存后不可修改（内置角色不可改）")
    name = forms.CharField(label="角色名称", max_length=50, widget=forms.TextInput(BOOTSTRAP_INPUT))
    description = forms.CharField(label="角色说明", required=False,
                                  widget=forms.Textarea({"class": "form-control", "rows": 2}))
    is_active = forms.BooleanField(label="启用", required=False, initial=True,
                                   widget=forms.CheckboxInput({"class": "form-check-input"}))

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if not code.replace("_", "").isalnum():
            raise forms.ValidationError("编码只能包含字母、数字和下划线。")
        return code
