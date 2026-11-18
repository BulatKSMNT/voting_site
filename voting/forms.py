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
    mode = forms.ChoiceField(label='Тип тура', choices=[('individual', 'Индивидуальные раунды ДА/НЕТ'),
                                                      ('standard', 'Общее голосование')], required=False, initial='individual')
    winners_count = forms.IntegerField(label='Лучших уровней баллов (N)', min_value=1, max_value=100, required=False, initial=8)
    names = forms.CharField(label='Участники (можно добавить позже)', required=False,
                           widget=forms.Textarea(attrs={'rows': 12, 'cols': 50}),
                           help_text='Фамилия и имя, каждый с новой строки. Можно начать с одного участника.')


class EventControlForm(forms.Form):
    action = forms.ChoiceField(label='Действие')
    names = forms.CharField(label='Добавить участников', required=False, widget=forms.Textarea(attrs={'rows': 5}),
                           help_text='По одному имени на строку. В индивидуальном туре каждому создаётся отдельный раунд.')
    winners_count = forms.IntegerField(label='Лучших уровней баллов (N)', min_value=1, max_value=100, required=False)
    participant = forms.ModelChoiceField(label='Дополнительный победитель из участников', queryset=Participant.objects.none(), required=False)
    full_name = forms.CharField(label='Или фамилия и имя нового финалиста', max_length=200, required=False)
    tied = forms.ModelMultipleChoiceField(label='Проходящие участники при равенстве', queryset=Participant.objects.none(), required=False,
                                         widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, round_obj, **kwargs):
        super().__init__(*args, **kwargs)
        actions = []
        rd = round_obj
        if not rd.event_completed and not rd.ended_at:
            if rd.event_stage == 'qualification' or (rd.event_stage == 'semifinal' and rd.status == 'pending'):
                actions.append(('add', 'Добавить участника / список'))
            else:
                self.fields.pop('names')
            if rd.type == 'standard' and rd.event_stage != 'final':
                actions.append(('configure', 'Сохранить уровни баллов: топ-N'))
                self.fields['winners_count'].initial = rd.winners_count
            else:
                self.fields.pop('winners_count')
        else:
            self.fields.pop('names')
            self.fields.pop('winners_count')
        if rd.event_stage and not rd.event_completed:
            if rd.status == 'pending':
                actions.append(('start', 'Открыть голосование'))
            elif not rd.ended_at or rd.event_stage != 'final':
                actions.append(('finish', 'Завершить этот раунд' if rd.event_stage == 'performance' else 'Завершить ТУР и перенести топ-N'))
            summary = results(rd)
            if rd.ended_at and summary['tie_slots']:
                actions.append(('tie', f"Разрешить равенство: выбрать {summary['tie_slots']}"))
                self.fields['tied'].queryset = rd.participants.filter(pk__in=[p['id'] for p in summary['tie_candidates']])
        if rd.type == 'standard' and rd.event_stage in {'', 'semifinal'} and (not rd.next_round_id or rd.next_round.status == 'pending'):
            actions.extend([('jury', 'Выбрать дополнительного победителя'), ('clear', 'Убрать дополнительного победителя')])
            self.fields['participant'].queryset = rd.participants.all()
        else:
            self.fields.pop('participant')
            self.fields.pop('full_name')
        if not any(code == 'tie' for code, label in actions):
            self.fields.pop('tied')
        self.fields['action'].choices = actions

    def clean(self):
        data = super().clean()
        if data.get('action') == 'add' and not data.get('names', '').strip():
            self.add_error('names', 'Укажите хотя бы одно имя.')
        if data.get('action') == 'configure' and data.get('winners_count') is None:
            self.add_error('winners_count', 'Укажите число лучших уровней баллов.')
        return data
