from django import forms
from .models import KnowledgeDocument, KnowledgeCategory


class KnowledgeDocumentForm(forms.ModelForm):
    class Meta:
        model = KnowledgeDocument
        fields = ['title', 'category', 'content', 'summary', 'tags', 'status']
        widgets = {
            'content': forms.Textarea(attrs={'rows': 15, 'class': 'form-control'}),
            'summary': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-control'}),
            'tags': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '多个标签用逗号分隔'}),
            'status': forms.Select(attrs={'class': 'form-control'}),
        }


class KnowledgeCategoryForm(forms.ModelForm):
    class Meta:
        model = KnowledgeCategory
        fields = ['name', 'parent', 'description', 'sort_order']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'parent': forms.Select(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
        }
