from django import forms
from .models import Decoration, CustomerCredit, CustomerComplaint, Regulation, PriceEvaluation, Insurance, Complaint


class DecorationForm(forms.ModelForm):
    """装修记录表单"""
    class Meta:
        model = Decoration
        fields = ['project_name', 'constructor', 'project_address', 'start_date', 'original_price', 'final_price']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'project_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入项目名称'}),
            'constructor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入施工方'}),
            'project_address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入项目地址'}),
            'original_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入原价', 'step': '0.01'}),
            'final_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入核后价', 'step': '0.01'}),
        }
        labels = {
            'project_name': '项目名称',
            'constructor': '施工方',
            'project_address': '项目地址',
            'start_date': '开工日期',
            'original_price': '原价',
            'final_price': '核后价',
        }
    
    def clean_project_name(self):
        project_name = self.cleaned_data.get('project_name')
        if not project_name:
            raise forms.ValidationError('项目名称不能为空')
        return project_name
    
    def clean_constructor(self):
        constructor = self.cleaned_data.get('constructor')
        if not constructor:
            raise forms.ValidationError('施工方不能为空')
        return constructor
    
    def clean_start_date(self):
        start_date = self.cleaned_data.get('start_date')
        if not start_date:
            raise forms.ValidationError('开工日期不能为空')
        return start_date
    
    def clean_final_price(self):
        final_price = self.cleaned_data.get('final_price')
        if final_price is None:
            raise forms.ValidationError('核后价不能为空')
        return final_price


class CustomerCreditForm(forms.ModelForm):
    """客户账期表单"""
    class Meta:
        model = CustomerCredit
        fields = ['proposal_date', 'site_date', 'customer_name', 'customer_type', 'salesperson', 'work_content', 'specific_content', 'pre_survey', 'work_method', 'result', 'remarks', 'has_assessment_period', 'assessment_start_date', 'assessment_end_date', 'assessment_content']
        widgets = {
            'proposal_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'site_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'customer_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入客户名称'}),
            'customer_type': forms.Select(attrs={'class': 'form-select'}),
            'salesperson': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入业务员'}),
            'work_content': forms.Select(attrs={'class': 'form-select'}),
            'specific_content': forms.Select(attrs={'class': 'form-select'}),
            'pre_survey': forms.Select(attrs={'class': 'form-select'}),
            'work_method': forms.Select(attrs={'class': 'form-select'}),
            'result': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入结果'}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入备注信息', 'rows': 3, 'maxlength': 500}),
            'has_assessment_period': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'assessment_start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'assessment_end_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'assessment_content': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入考核内容描述', 'rows': 2, 'maxlength': 500}),
        }
        labels = {
            'proposal_date': '提出日期',
            'site_date': '现场日期',
            'customer_name': '客户名称',
            'customer_type': '客户类型',
            'salesperson': '业务员',
            'work_content': '工作内容',
            'specific_content': '具体内容',
            'pre_survey': '前期调查表',
            'work_method': '工作方式',
            'result': '结果',
            'has_assessment_period': '是否有业绩考核期',
            'assessment_start_date': '考核期开始日期',
            'assessment_end_date': '考核期结束日期',
            'assessment_content': '考核内容',
        }
    
    def clean_proposal_date(self):
        proposal_date = self.cleaned_data.get('proposal_date')
        if not proposal_date:
            raise forms.ValidationError('提出日期不能为空')
        return proposal_date
    
    def clean_customer_name(self):
        customer_name = self.cleaned_data.get('customer_name')
        if not customer_name:
            raise forms.ValidationError('客户名称不能为空')
        return customer_name
    
    def clean_salesperson(self):
        salesperson = self.cleaned_data.get('salesperson')
        if not salesperson:
            raise forms.ValidationError('业务员不能为空')
        return salesperson
    
    def clean_customer_type(self):
        customer_type = self.cleaned_data.get('customer_type')
        if not customer_type:
            raise forms.ValidationError('客户类型不能为空')
        return customer_type
    
    def clean_result(self):
        result = self.cleaned_data.get('result')
        if not result:
            raise forms.ValidationError('结果不能为空')
        return result


class CustomerComplaintForm(forms.ModelForm):
    """客户客诉表单

    complaint_status 已从 fields 中移除——状态只能由闭环动作推导，
    编辑表单禁止直接改状态。新增时模型 default='未完成' 自动生效。
    """
    class Meta:
        model = CustomerComplaint
        fields = ['customer_name', 'responsible_salesperson', 'complaint_date', 'terminal_customer', 'product_model', 'our_shipment', 'customer_shipment', 'complaint_description', 'compensation_amount', 'is_insured', 'engineering_report', 'engineering_analysis', 'missing_materials', 'follow_up_status', 'remarks', 'closure_result', 'closure_note', 'sla_hours']
        widgets = {
            'complaint_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'customer_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入客户名称'}),
            'responsible_salesperson': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入责任业务员'}),
            'terminal_customer': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入终端客户'}),
            'product_model': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入产品型号'}),
            'our_shipment': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入我司出货量'}),
            'customer_shipment': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入客户出货量'}),
            'complaint_description': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入客诉现象及客户诉求描述', 'rows': 3}),
            'compensation_amount': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入客户诉求（赔偿金额）', 'step': '0.01'}),
            'is_insured': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'engineering_report': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入工程报告', 'rows': 3}),
            'engineering_analysis': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入工程部分析原因报告', 'rows': 3}),
            'missing_materials': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入欠缺材料'}),
            'follow_up_status': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入跟进情况', 'rows': 3}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入备注', 'rows': 3}),
            'closure_result': forms.Select(attrs={'class': 'form-select'}),
            'closure_note': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入闭环说明', 'rows': 3}),
            'sla_hours': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'SLA时限（小时），0 表示不考核', 'min': '0'}),
        }
        labels = {
            'customer_name': '客户名称',
            'responsible_salesperson': '责任业务员',
            'complaint_date': '客诉申请日期',
            'terminal_customer': '终端客户',
            'product_model': '产品型号',
            'our_shipment': '我司出货量',
            'customer_shipment': '客户出货量',
            'complaint_description': '客诉现象及客户诉求描述',
            'compensation_amount': '客户诉求（赔偿金额）',
            'is_insured': '是否报保险',
            'engineering_report': '工程报告',
            'engineering_analysis': '工程部分析原因报告',
            'missing_materials': '欠缺材料',
            'follow_up_status': '跟进情况',
            'remarks': '备注',
            'closure_result': '闭环结论',
            'closure_note': '闭环说明',
            'sla_hours': 'SLA时限(小时)',
        }
    
    def clean_customer_name(self):
        customer_name = self.cleaned_data.get('customer_name')
        if not customer_name:
            raise forms.ValidationError('客户名称不能为空')
        return customer_name
    
    def clean_responsible_salesperson(self):
        responsible_salesperson = self.cleaned_data.get('responsible_salesperson')
        if not responsible_salesperson:
            raise forms.ValidationError('责任业务员不能为空')
        return responsible_salesperson
    
    def clean_complaint_date(self):
        complaint_date = self.cleaned_data.get('complaint_date')
        if not complaint_date:
            raise forms.ValidationError('客诉申请日期不能为空')
        return complaint_date


class CustomerComplaintClosureForm(forms.Form):
    """客诉闭环操作表单（仅闭环结论与说明，闭环时间/人由视图自动回填）。"""
    closure_result = forms.ChoiceField(
        label='闭环结论',
        choices=[('accepted', '客户接受'), ('partial', '部分接受'), ('rejected', '不接受')],
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    closure_note = forms.CharField(
        label='闭环说明', required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': '记录最终处理结果与客户反馈……'}),
    )


class RegulationForm(forms.ModelForm):
    """制度及处罚表单"""
    class Meta:
        model = Regulation
        fields = ['issue_date', 'title', 'content', 'doc_no']
        widgets = {
            'issue_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入标题'}),
            'content': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入主要内容', 'rows': 5}),
            'doc_no': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入文件编号'}),
        }
        labels = {
            'issue_date': '出文日期',
            'title': '标题',
            'content': '主要内容',
            'doc_no': '文件编号',
        }
    
    def clean_issue_date(self):
        issue_date = self.cleaned_data.get('issue_date')
        if not issue_date:
            raise forms.ValidationError('出文日期不能为空')
        return issue_date
    
    def clean_title(self):
        title = self.cleaned_data.get('title')
        if not title:
            raise forms.ValidationError('标题不能为空')
        return title
    
    def clean_content(self):
        content = self.cleaned_data.get('content')
        if not content:
            raise forms.ValidationError('主要内容不能为空')
        return content
    
    def clean_doc_no(self):
        doc_no = self.cleaned_data.get('doc_no')
        if not doc_no:
            raise forms.ValidationError('文件编号不能为空')
        return doc_no


class PriceEvaluationForm(forms.ModelForm):
    """核价表单"""
    class Meta:
        model = PriceEvaluation
        fields = ['project_category', 'proposing_department', 'proposer', 'evaluation_content', 'supplier1_name', 'supplier1_price', 'supplier2_name', 'supplier2_price', 'supplier3_name', 'supplier3_price', 'internal_control_supplier', 'internal_control_price']
        widgets = {
            'project_category': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入项目分类'}),
            'proposing_department': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入项目提出部门'}),
            'proposer': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入提出人'}),
            'evaluation_content': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入核价内容', 'rows': 3}),
            'supplier1_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入供应商1名称'}),
            'supplier1_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入供应商1报价', 'step': '0.01'}),
            'supplier2_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入供应商2名称'}),
            'supplier2_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入供应商2报价', 'step': '0.01'}),
            'supplier3_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入供应商3名称'}),
            'supplier3_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入供应商3报价', 'step': '0.01'}),
            'internal_control_supplier': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入内控审核供应商'}),
            'internal_control_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入内控审核价', 'step': '0.01'}),
        }
        labels = {
            'project_category': '项目分类',
            'proposing_department': '项目提出部门',
            'proposer': '提出人',
            'evaluation_content': '核价内容',
            'supplier1_name': '供应商1名称',
            'supplier1_price': '供应商1报价',
            'supplier2_name': '供应商2名称',
            'supplier2_price': '供应商2报价',
            'supplier3_name': '供应商3名称',
            'supplier3_price': '供应商3报价',
            'internal_control_supplier': '内控审核供应商',
            'internal_control_price': '内控审核价',
        }
    
    def clean_project_category(self):
        project_category = self.cleaned_data.get('project_category')
        if not project_category:
            raise forms.ValidationError('项目分类不能为空')
        return project_category
    
    def clean_proposing_department(self):
        proposing_department = self.cleaned_data.get('proposing_department')
        if not proposing_department:
            raise forms.ValidationError('项目提出部门不能为空')
        return proposing_department
    
    def clean_proposer(self):
        proposer = self.cleaned_data.get('proposer')
        if not proposer:
            raise forms.ValidationError('提出人不能为空')
        return proposer
    
    def clean_internal_control_price(self):
        internal_control_price = self.cleaned_data.get('internal_control_price')
        if internal_control_price is None:
            raise forms.ValidationError('内控审核价不能为空')
        return internal_control_price


class InsuranceForm(forms.ModelForm):
    """保险表单"""
    class Meta:
        model = Insurance
        fields = ['company_name', 'title', 'order_number', 'premium', 'issue_date', 'end_date', 'department', 'remarks']
        widgets = {
            'issue_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'end_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'company_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入公司名称'}),
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入标题'}),
            'order_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入单号'}),
            'premium': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入保费', 'step': '0.01'}),
            'department': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入部门'}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入备注', 'rows': 3}),
        }
        labels = {
            'company_name': '公司名称',
            'title': '标题',
            'order_number': '单号',
            'premium': '保费',
            'issue_date': '签发日期',
            'end_date': '截止日期',
            'department': '部门',
            'remarks': '备注',
        }
    
    def clean_company_name(self):
        company_name = self.cleaned_data.get('company_name')
        if not company_name:
            raise forms.ValidationError('公司名称不能为空')
        return company_name
    
    def clean_title(self):
        title = self.cleaned_data.get('title')
        if not title:
            raise forms.ValidationError('标题不能为空')
        return title
    
    def clean_premium(self):
        premium = self.cleaned_data.get('premium')
        if premium is None:
            raise forms.ValidationError('保费不能为空')
        return premium
    
    def clean_issue_date(self):
        issue_date = self.cleaned_data.get('issue_date')
        if not issue_date:
            raise forms.ValidationError('签发日期不能为空')
        return issue_date
    
    def clean_end_date(self):
        end_date = self.cleaned_data.get('end_date')
        if not end_date:
            raise forms.ValidationError('截止日期不能为空')
        return end_date
    
    def clean_department(self):
        department = self.cleaned_data.get('department')
        if not department:
            raise forms.ValidationError('部门不能为空')
        return department


class ComplaintForm(forms.ModelForm):
    """客诉表单"""
    class Meta:
        model = Complaint
        fields = ['dispute_type', 'defendant', 'case_number', 'amount', 'court', 'lawyer', 'progress', 'detailed_progress', 'remarks', 'filed_at', 'hearing_at', 'judgment_at', 'execution_at', 'closed_at']
        widgets = {
            'dispute_type': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入纠纷类型'}),
            'defendant': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入被告'}),
            'case_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入案号'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '请输入涉案金额', 'step': '0.01'}),
            'court': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入受理法院'}),
            'lawyer': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入代理律师'}),
            'progress': forms.Select(attrs={'class': 'form-select'}),
            'detailed_progress': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入详细进度', 'rows': 3}),
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'placeholder': '请输入备注', 'rows': 3}),
            'filed_at': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'hearing_at': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'judgment_at': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'execution_at': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'closed_at': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
        }
        labels = {
            'dispute_type': '纠纷类型',
            'defendant': '被告',
            'case_number': '案号',
            'amount': '涉案金额（元）',
            'court': '受理法院',
            'lawyer': '代理律师',
            'progress': '进度',
            'detailed_progress': '详细进度',
            'remarks': '备注',
            'filed_at': '立案日期',
            'hearing_at': '开庭日期',
            'judgment_at': '判决日期',
            'execution_at': '执行日期',
            'closed_at': '结案日期',
        }
    
    def clean_dispute_type(self):
        dispute_type = self.cleaned_data.get('dispute_type')
        if not dispute_type:
            raise forms.ValidationError('纠纷类型不能为空')
        return dispute_type
    
    def clean_defendant(self):
        defendant = self.cleaned_data.get('defendant')
        if not defendant:
            raise forms.ValidationError('被告不能为空')
        return defendant
    
    def clean_case_number(self):
        case_number = self.cleaned_data.get('case_number')
        if not case_number:
            raise forms.ValidationError('案号不能为空')
        return case_number
    
    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None:
            raise forms.ValidationError('涉案金额不能为空')
        if amount <= 0:
            raise forms.ValidationError('涉案金额必须大于0')
        return amount
    
    def clean_court(self):
        court = self.cleaned_data.get('court')
        if not court:
            raise forms.ValidationError('受理法院不能为空')
        return court
    
    def clean_lawyer(self):
        lawyer = self.cleaned_data.get('lawyer')
        if not lawyer:
            raise forms.ValidationError('代理律师不能为空')
        return lawyer
    
    def clean_progress(self):
        progress = self.cleaned_data.get('progress')
        if not progress:
            raise forms.ValidationError('进度不能为空')
        return progress