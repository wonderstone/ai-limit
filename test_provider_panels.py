"""Related provider sections stay in one framed layout unit."""
import copy
import unittest
from unittest import mock
from test_widget_layout import menubar, _Panel


def sections():
    def header(name,family,order):
        return {'type':'section','name':name,'color':'#c4b5fd','family':family,
                'family_label':'Claude' if family=='claude' else 'OpenAI',
                'subsection_label':name,'subsection_order':order}
    row=lambda name:{'name':name,'kind':'credit','value':'$200.00','color':'#fff','reset':None,'pct':None}
    return [header('Subscription & cloud','claude',0),row('Cloud credits'),
            header('API credits','openai',1),row('OpenAI promotional'),
            {'type':'section','name':'Grok','color':'#fff'},row('Grok balance'),
            header('API credits','claude',1),row('Claude promotional'),
            header('Codex usage & credits','openai',0),row('Codex credits')]


class ProviderPanelTests(unittest.TestCase):
    def test_related_sections_are_atomic_and_ordered_without_mutation(self):
        rows=sections();before=copy.deepcopy(rows)
        blocks=menubar.AiLimitApp._widget_detail_blocks(rows)
        self.assertEqual(rows,before)
        self.assertEqual([b[0]['name'] for b in blocks],['Claude','OpenAI','Grok'])
        self.assertEqual([r['name'] for r in blocks[0] if r.get('type')=='subsection'],['Subscription & cloud','API credits'])
        self.assertEqual([r['name'] for r in blocks[1] if r.get('type')=='subsection'],['Codex usage & credits','API credits'])

    def test_provider_is_never_split_between_columns(self):
        app=menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        for count in (1,2):
            groups,heights=app._widget_detail_columns(sections(),count,290)
            family_blocks=[b for col in groups for b in col if b[0].get('type')=='provider']
            self.assertEqual(len(family_blocks),2)
            self.assertTrue(all(len([r for r in b if r.get('type')=='subsection'])==2 for b in family_blocks))
            self.assertTrue(all(h>0 for h in heights))

    def test_native_frame_contains_all_its_rows_at_both_widths(self):
        app=menubar.AiLimitApp.__new__(menubar.AiLimitApp);app._state={'lang':'en'}
        for width in (420,640):
            app._widget_panel=_Panel(width=width,height=900);app._widget_content=mock.MagicMock()
            labels=[];boxes=[]
            with mock.patch.object(app,'_widget_detail_rows',return_value=sections()), mock.patch.object(app,'_widget_alert_rows',return_value=[]), \
                 mock.patch.object(app,'_widget_resize_panel'),mock.patch.object(app,'_widget_keep_top_visible'),mock.patch.object(app,'_clear_widget_content'), \
                 mock.patch.object(app,'_widget_add_label',side_effect=lambda text,x,y,w,h,**kw:labels.append((text,x,y,w,h))), \
                 mock.patch.object(app,'_widget_add_box',side_effect=lambda x,y,w,h,*args,**kw:boxes.append((x,y,w,h,kw))):
                app._render_widget_dashboard()
            frames=[b for b in boxes if b[4].get('border')]
            self.assertEqual(len(frames),2)
            for names in [('Claude','Cloud credits','Claude promotional'),('OpenAI','OpenAI promotional','Codex credits')]:
                matching=[label for label in labels if label[0] in names]
                self.assertEqual(len(matching),3)
                self.assertTrue(any(all(x>=bx and y>=by and x+w<=bx+bw and y+h<=by+bh for _,x,y,w,h in matching) for bx,by,bw,bh,_ in frames))


if __name__=='__main__':unittest.main()
