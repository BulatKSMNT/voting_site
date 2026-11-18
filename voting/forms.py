from django import forms
from .access import parse_telegram_ids
from .models import Campaign
from .models import Participant
from .audit import results


class CampaignAdminForm(forms.ModelForm):
    allowed_telegram_ids = forms.CharField(
        label="Допущенные Telegram ID", required=False, widget=forms.Textarea(attrs={"rows": 10, "cols": 45}),
        help_text="По одному ID на строку или через пробел, запятую, точку с запятой. "
                  "В приватном режиме пустой список запрещает голосовать всем. "
                  "В общедоступном режиме список сохраняется, но не ограничивает голосование.",
    )

    class Meta:
        model = Campaign
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial['allowed_telegram_ids'] = '\n'.join(str(uid) for uid in self.instance.allowed_telegram_ids)

    def clean_allowed_telegram_ids(self):
        try:
            return parse_telegram_ids(self.cleaned_data['allowed_telegram_ids'])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


class EventPrepareForm(forms.Form):
    names = forms.CharField(label='Участники первого тура', widget=forms.Textarea(attrs={'rows': 25, 'cols': 50}),
                           help_text='Фамилия и имя, каждый с новой строки. Для мероприятия — 25 человек.')


class EventControlForm(forms.Form):
    action = forms.ChoiceField(label='Действие')
    participant = forms.ModelChoiceField(label='Дополнительный победитель из участников', queryset=Participant.objects.none(), required=False)
    full_name = forms.CharField(label='Или фамилия и имя нового финалиста', max_length=200, required=False)
    tied = forms.ModelMultipleChoiceField(label='Проходящие участники при равенстве', queryset=Participant.objects.none(), required=False,
                                         widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, round_obj, **kwargs):
        super().__init__(*args, **kwargs)
        actions = []
        if round_obj.event_stage and not round_obj.event_completed:
            if round_obj.status == 'pending':
                actions.append(('start', 'Открыть голосование'))
            else:
                actions.append(('finish', 'Завершить тур и перенести проходящих'))
            summary = results(round_obj)
            if round_obj.ended_at and summary['tie_slots']:
                actions.append(('tie', f"Разрешить равенство: выбрать {summary['tie_slots']}"))
                self.fields['tied'].queryset = round_obj.participants.filter(pk__in=[p['id'] for p in summary['tie_candidates']])
        if round_obj.type == 'standard' and round_obj.event_stage in {'', 'semifinal'}:
            actions.extend([('jury', 'Выбрать дополнительного победителя'), ('clear', 'Убрать дополнительного победителя')])
            self.fields['participant'].queryset = round_obj.participants.all()
        self.fields['action'].choices = actions
