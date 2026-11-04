from django import forms
from django.core.files.storage import default_storage

from work_record.file_validation import content_matches, normalize_ext, read_head

from .models import (CustomerComplaint, CustomerCredit, CustomerLedger, CustomerReview,
                     Decoration, Insurance, Complaint, PaymentWalkthrough,
                     PriceEvaluation, Regulation)


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
    # 客户细分：与客户评审单 customer_type 同档位，直通取值（方案定稿修正 2）。
    # 模型层 blank=True 兼容历史 70 张；表单层必填，历史单编辑时补选。
    customer_segment = forms.ChoiceField(
        label='客户细分', required=True,
        choices=[('', '请选择')] + list(CustomerReview.CUSTOMER_TYPES),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = CustomerCredit
        fields = ['proposal_date', 'site_date', 'customer_name', 'customer_type', 'customer_segment', 'approved_terms_days', 'approved_limit', 'salesperson', 'work_content', 'specific_content', 'pre_survey', 'work_method', 'result', 'remarks', 'has_assessment_period', 'assessment_start_date', 'assessment_end_date', 'assessment_content']
        widgets = {
            'proposal_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'site_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'customer_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入客户名称', 'list': 'customer-roster'}),
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
            'approved_terms_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'max': 3650, 'step': 1, 'placeholder': '如 60'}),
            'approved_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': '0.01', 'placeholder': '单位：万元'}),
        }
        labels = {
            'approved_terms_days': '批准账期（天）',
            'approved_limit': '批准额度（万元）',
            'proposal_date': '提出日期',
            'site_date': '现场日期',
            'customer_name': '客户名称',
            'customer_type': '客户类型',
            'salesperson': '业务员',            'work_content': '工作内容',
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
    
    def clean_approved_terms_days(self):
        days = self.cleaned_data.get('approved_terms_days')
        if days is not None and (days < 0 or days > 3650):
            raise forms.ValidationError('批准账期须在 0～3650 天之间')
        return days

    def clean_approved_limit(self):
        limit = self.cleaned_data.get('approved_limit')
        if limit is not None and limit < 0:
            raise forms.ValidationError('批准额度不能为负数')
        return limit

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


class CustomerLedgerForm(forms.ModelForm):
    """客户台账表单。

    附件规则：单条记录只允许一个 PDF（营业执照/法人身份证/基本合同等合并扫描），
    扩展名 + 20MB 上限 + 文件头三重校验；替换或清空时删除旧文件。
    """

    ATTACHMENT_MAX_SIZE = 20 * 1024 * 1024

    class Meta:
        model = CustomerLedger
        fields = [
            'responsible_salesperson', 'customer_name', 'customer_category',
            'legal_representative', 'address', 'credit_code',
            'registered_capital', 'registered_capital_text',
            'paid_in_capital', 'paid_in_capital_text', 'company_status',
            'business_license', 'payment_terms',
            'credit_limit', 'credit_limit_text', 'change_date',
            'basic_contract', 'contract_sign_date', 'contract_issues',
            'asset_guarantee', 'legal_id_card', 'guarantor_id_card',
            'asset_guarantor', 'guaranteed_asset_detail', 'asset_mortgaged',
            'asset_guarantee_issues', 'contract_count',
            'handling_suggestion', 'system_info', 'location',
            'archive_no', 'archive_date', 'remarks', 'attachment',
        ]
        widgets = {
            'responsible_salesperson': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '请输入责任业务人员',
            }),
            'customer_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '请输入客户名称',
            }),
            'customer_category': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 经销商 / 终端商',
            }),
            'legal_representative': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '请输入法人姓名',
            }),
            'address': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '请输入注册/经营地址',
            }),
            'credit_code': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '请输入统一社会信用代码',
            }),
            'registered_capital': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'placeholder': '万元',
            }),
            'registered_capital_text': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '原文，如 50万 / 未披露',
            }),
            'paid_in_capital': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'placeholder': '万元',
            }),
            'paid_in_capital_text': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '原文，如 37万 / 查不到',
            }),
            'company_status': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 存续 / 注销',
            }),
            'business_license': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 旧（2017年）无 / 过期（2008年）',
            }),
            'payment_terms': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 月结30天',
            }),
            'credit_limit': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'placeholder': '万元',
            }),
            'credit_limit_text': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '原文，如 80万',
            }),
            'change_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'basic_contract': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 有 / 无',
            }),
            'contract_sign_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'contract_issues': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '基本合同存在的问题',
            }),
            'asset_guarantee': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 有 / 无',
            }),
            'legal_id_card': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '法代身份证情况',
            }),
            'guarantor_id_card': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '担保人身份证情况',
            }),
            'asset_guarantor': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '资产担保人',
            }),
            'guaranteed_asset_detail': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '担保资产明细',
            }),
            'asset_mortgaged': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 是 / 否',
            }),
            'asset_guarantee_issues': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '资产担保存在的问题',
            }),
            'contract_count': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 1 / 2 / /',
            }),
            'handling_suggestion': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '处理建议',
            }),
            'system_info': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '系统信息',
            }),
            'location': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '如 12-2',
            }),
            'archive_no': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '唯一档案编号，可留空',
            }),
            'archive_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'remarks': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3, 'placeholder': '备注信息',
            }),
            'attachment': forms.ClearableFileInput(attrs={
                'class': 'form-control', 'accept': '.pdf,application/pdf',
            }),
        }

    def clean_archive_no(self):
        value = (self.cleaned_data.get('archive_no') or '').strip()
        return value or None

    def clean_attachment(self):
        """仅校验新上传文件；未变更时直接返回原值。"""
        new_file = self.files.get('attachment')
        if not new_file:
            return self.cleaned_data.get('attachment')
        ext = normalize_ext(new_file.name)
        if ext != 'PDF':
            raise forms.ValidationError('附件仅支持 PDF 格式（请合并为一个 PDF 后上传）')
        if new_file.size > self.ATTACHMENT_MAX_SIZE:
            raise forms.ValidationError(
                f'附件大小 {new_file.size / 1024 / 1024:.1f}MB 超过上限 '
                f'{self.ATTACHMENT_MAX_SIZE // 1024 // 1024}MB'
            )
        if not content_matches(new_file, 'PDF', read_head(new_file)):
            raise forms.ValidationError('文件内容与 PDF 扩展名不符，已拒绝上传')
        return new_file

    def save(self, commit=True):
        """保存成功后清理被替换/清空的旧附件，避免磁盘残留孤儿文件。"""
        old_name = None
        if self.instance.pk:
            old_name = (
                CustomerLedger.objects.filter(pk=self.instance.pk)
                .values_list('attachment', flat=True)
                .first()
            )
        obj = super().save(commit=commit)
        new_name = obj.attachment.name if obj.attachment else ''
        if commit and old_name and old_name != new_name:
            default_storage.delete(old_name)
        return obj

# ============================================================================
# 客户评审 / 付款穿行测试
# ============================================================================

_DATE = {'type': 'date'}
_TEXT = {'class': 'form-control'}
_NUM = {'class': 'form-control', 'step': '0.01', 'inputmode': 'decimal'}


class CustomerReviewForm(forms.ModelForm):
    """客户评审主表表单。

    只承载「表头 + 财务基础数据 + 结论」；评分项不走 Django 字段 ——
    25 个评分项的档位是动态的，硬编码成 ChoiceField 会让新增/调整档位必改两处。
    评分在视图里按 records/credit_spec.py 的规则解析并收敛。
    """

    class Meta:
        model = CustomerReview
        fields = [
            'review_no', 'customer_name', 'customer_code', 'customer_type', 'review_date',
            'period', 'settle_method', 'region', 'prev_grade', 'prev_limit',
            'apply_no', 'audit_address', 'companion',
            'total_assets', 'total_liabilities', 'current_assets', 'current_liabilities',
            'inventory', 'accounts_receivable', 'cash', 'interest_debt',
            'revenue', 'prev_revenue', 'net_profit', 'operating_cash_flow',
            'conclusion', 'credit_limit', 'credit_days', 'reviewer', 'approver',
        ]
        widgets = {
            'review_no': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 XY-2026-001'}),
            'customer_name': forms.TextInput(attrs={**_TEXT, 'placeholder': '与营业执照一致的全称'}),
            'customer_code': forms.TextInput(attrs={**_TEXT, 'placeholder': 'ERP 客户编码'}),
            'customer_type': forms.Select(attrs={'class': 'form-select'}),
            'review_date': forms.DateInput(attrs={**_TEXT, **_DATE}, format='%Y-%m-%d'),
            'period': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 2026 年度 / 2026 年第 3 季度'}),
            'settle_method': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 月结 60 天'}),
            'region': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 华东-江苏'}),
            'prev_grade': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 B 级'}),
            'prev_limit': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'apply_no': forms.TextInput(attrs={**_TEXT, 'placeholder': '对应的准入申请编号'}),
            'audit_address': forms.TextInput(attrs={**_TEXT, 'placeholder': '实际经营地址'}),
            'companion': forms.TextInput(attrs={**_TEXT, 'placeholder': '客户方陪同人员'}),
            'total_assets': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'total_liabilities': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'current_assets': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'current_liabilities': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'inventory': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'accounts_receivable': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'cash': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'interest_debt': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'revenue': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'prev_revenue': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'net_profit': forms.NumberInput(attrs={**_NUM, 'placeholder': '亏损填负数'}),
            'operating_cash_flow': forms.NumberInput(attrs={**_NUM, 'placeholder': '可为负数'}),
            'conclusion': forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                                'placeholder': '结论、授信意见与整改要求'}),
            'credit_limit': forms.NumberInput(attrs={**_NUM, 'placeholder': '万元'}),
            'credit_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': 1,
                                                    'placeholder': '天'}),
            'reviewer': forms.TextInput(attrs={**_TEXT, 'placeholder': '编制人姓名'}),
            'approver': forms.TextInput(attrs={**_TEXT, 'placeholder': '审批人姓名'}),
        }
        labels = {
            'review_no': '表单编号',
            'customer_name': '客户全称',
            'customer_code': '客户编码',
            'customer_type': '客户类型',
            'review_date': '表单日期',
            'period': '考评周期',
            'settle_method': '结算方式',
            'region': '合作区域',
            'prev_grade': '原信用等级',
            'prev_limit': '原授信额度',
            'apply_no': '对应申请编号',
            'audit_address': '审核地址',
            'companion': '陪同人员',
            'total_assets': '资产总额',
            'total_liabilities': '负债总额',
            'current_assets': '流动资产',
            'current_liabilities': '流动负债',
            'inventory': '存货',
            'accounts_receivable': '应收账款',
            'cash': '货币资金',
            'interest_debt': '有息负债',
            'revenue': '营业收入（本期）',
            'prev_revenue': '营业收入（上期）',
            'net_profit': '净利润',
            'operating_cash_flow': '经营活动现金流量净额',
            'conclusion': '结论与整改要求',
            'credit_limit': '建议信用额度',
            'credit_days': '建议信用期限',
            'reviewer': '编制人',
            'approver': '审批人',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 财务字段打上 data-fin，前端据此实时算比率；属性写在这里而不是模板，
        # 保证「字段名 → data 属性」只有一处定义。
        from .credit_spec import FIN_FIELDS
        for item in FIN_FIELDS:
            field = self.fields.get(item['key'])
            if field is not None:
                field.widget.attrs['data-fin'] = item['key']
        # 客户全称、日期是硬性必填，前端也标 *
        self.fields['customer_name'].required = True
        self.fields['review_date'].required = True
        for name in ('customer_code', 'period', 'settle_method', 'region', 'prev_grade',
                     'apply_no', 'audit_address', 'companion', 'conclusion',
                     'reviewer', 'approver', 'review_no'):
            self.fields[name].required = False
        for name in ('total_assets', 'total_liabilities', 'current_assets', 'current_liabilities',
                     'inventory', 'accounts_receivable', 'cash', 'interest_debt',
                     'revenue', 'prev_revenue', 'net_profit', 'operating_cash_flow',
                     'prev_limit', 'credit_limit', 'credit_days'):
            self.fields[name].required = False

    def clean(self):
        data = super().clean()
        # 资产/负债同时填了才校验勾稽关系；只填一个时交给模板标「数据不完整」
        ta, tl = data.get('total_assets'), data.get('total_liabilities')
        if ta is not None and tl is not None and ta > 0 and tl > ta * 3:
            self.add_error('total_liabilities', '负债总额超过资产总额 3 倍，请核对是否填错单位（万元）')
        ca = data.get('current_assets')
        # 用整数倍比较，避免 Decimal 与 float 混算抛 TypeError
        if ta and ca is not None and ca * 2 > ta * 3:
            self.add_error('current_assets', '流动资产超过资产总额，请核对是否填错单位（万元）')
        return data


class PaymentWalkthroughForm(forms.ModelForm):
    """付款穿行测试表单。样本明细由视图单独解析（JSON 字段）。"""

    class Meta:
        model = PaymentWalkthrough
        fields = [
            'test_no', 'payee_name', 'payee_type', 'period_start', 'period_end', 'test_date',
            'sample_method', 'population_size', 'sample_size',
            'terms_text', 'terms_days', 'terms_base',
            'conclusion', 'defect_summary', 'suggestion', 'tester', 'reviewer',
        ]
        widgets = {
            'test_no': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 WT-2026-004'}),
            'payee_name': forms.TextInput(attrs={**_TEXT, 'placeholder': '客户或供应商全称'}),
            'payee_type': forms.Select(attrs={'class': 'form-select'}),
            'period_start': forms.DateInput(attrs={**_TEXT, **_DATE}, format='%Y-%m-%d'),
            'period_end': forms.DateInput(attrs={**_TEXT, **_DATE}, format='%Y-%m-%d'),
            'test_date': forms.DateInput(attrs={**_TEXT, **_DATE}, format='%Y-%m-%d'),
            'sample_method': forms.TextInput(attrs={**_TEXT, 'list': 'sample-method-list'}),
            'population_size': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': 1}),
            'sample_size': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': 1}),
            'terms_text': forms.TextInput(attrs={**_TEXT, 'placeholder': '如 月结 60 天 / 货到 30 天'}),
            'terms_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': 1,
                                                   'placeholder': '如 60'}),
            'terms_base': forms.TextInput(attrs={**_TEXT, 'list': 'terms-base-list'}),
            'conclusion': forms.TextInput(attrs={**_TEXT, 'readonly': 'readonly',
                                                  'placeholder': '由控制点执行情况自动判定'}),
            'defect_summary': forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                                    'placeholder': '逐条列明缺陷所在控制点、样本、具体表现'}),
            'suggestion': forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                                'placeholder': '整改措施、责任人、完成时限'}),
            'tester': forms.TextInput(attrs={**_TEXT, 'placeholder': '测试人姓名'}),
            'reviewer': forms.TextInput(attrs={**_TEXT, 'placeholder': '复核人姓名'}),
        }
        labels = {
            'test_no': '测试编号',
            'payee_name': '被测试对象',
            'payee_type': '对象类型',
            'period_start': '测试期间（起）',
            'period_end': '测试期间（止）',
            'test_date': '测试执行日期',
            'sample_method': '抽样方法',
            'population_size': '总体笔数',
            'sample_size': '抽样笔数',
            'terms_text': '账期约定',
            'terms_days': '约定账期天数',
            'terms_base': '账期起算口径',
            'conclusion': '测试结论',
            'defect_summary': '缺陷汇总',
            'suggestion': '整改建议',
            'tester': '测试人',
            'reviewer': '复核人',
        }

    def clean(self):
        data = super().clean()
        start, end = data.get('period_start'), data.get('period_end')
        if start and end and start > end:
            self.add_error('period_end', '测试期间结束日不能早于开始日')
        pop, sample = data.get('population_size'), data.get('sample_size')
        if pop is not None and sample is not None and sample > pop:
            self.add_error('sample_size', '抽样笔数不能大于总体笔数')
        days = data.get('terms_days')
        if days is not None and (days < 0 or days > 3650):
            self.add_error('terms_days', '约定账期天数应在 0～3650 之间')
        return data
