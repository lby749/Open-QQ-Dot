"""Conservative, deterministic labels when local model weights are not installed."""
import re


def scene(text):
    patterns = (
        ('conflict', r'别烦我|不要再联系|讨厌你'),
        ('emotion_low', r'难过|伤心|崩溃|好累|委屈'),
        ('celebration', r'太开心|成功了|考上了|赢了|好消息'),
        ('study_help', r'这道题|怎么解|求解|讲解|作业|证明'),
        ('greeting', r'^(早安|晚安|你好|嗨|hello|hi)[呀啊哦!！。\s]*$'),
        ('concrete_question', r'[?？]|怎么|为什么|多少|哪里|几点'),
        ('playful_tease', r'哈哈|嘿嘿|hhh|笑死'),
    )
    return next((label for label, pattern in patterns if re.search(pattern, text, re.I)), 'unknown')


class RuleClassifier:
    load_count = 0
    load_seconds = 0

    def classify_with_review(self, text, *, direction=None, context=(), following=()):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text must be a nonempty string')
        intent = 'other'
        for label, pattern in (
            ('refuse', r'不需要|不要了|不想|不用了|拒绝'),
            ('inquire', r'[?？]|怎么|为什么|多少|哪里|几点'),
            ('comfort', r'别难过|没关系|会好起来|抱抱'),
            ('banter', r'哈哈|hhh|笑死|开玩笑'),
            ('confirm', r'^(好的?|嗯嗯?|对|是的|收到|可以)[呀啊哦!！。\s]*$'),
            ('complain', r'烦死|气死|真倒霉'),
            ('share', r'今天|刚刚|我在|我去|我买'),
        ):
            if re.search(pattern, text, re.I):
                intent = label
                break
        emotion = 'other'
        for label, pattern in (('happy', r'happy|high|'), ('funny', r'哈哈|hhh|笑死'),
                               ('sad', r'难过|伤心|委屈'), ('angry', r'angry|'),
                               ('annoyed', r'烦死|好烦'), ('surprised', r'居然|震惊|天哪')):
            if re.search(pattern, text, re.I):
                emotion = label
                break
        return {'labels': {'intent': intent, 'emotion': emotion, 'tone': 'casual',
                           'initiative': 'passive' if intent in ('confirm', 'respond', '确认', '回应') else 'proactive'},
                'needs_review': True, 'reason': 'uncertain'}

    def classify(self, text, **kwargs):
        return self.classify_with_review(text, **kwargs)['labels']

    def classify_batch_with_review(self, texts, *, directions=None, contexts=None, following_contexts=None):
        texts = list(texts)
        if len(texts) > 64:
            raise ValueError('batch limited to 64 messages')
        companions = [list(values) if values is not None else [default] * len(texts)
                      for values, default in ((directions, None), (contexts, ()), (following_contexts, ()))]
        if any(len(values) != len(texts) for values in companions):
            raise ValueError('batch companion lengths must match')
        return [self.classify_with_review(text, direction=d, context=c, following=f)
                for text, d, c, f in zip(texts, *companions)]

    def classify_batch(self, texts, **kwargs):
        return [item['labels'] for item in self.classify_batch_with_review(texts, **kwargs)]

    def _ensure_loaded(self):
        from message_classifier import ClassifierUnavailable
        raise ClassifierUnavailable('本地模型已关闭或未安装；消息标签使用程序规则')

    def close(self):
        pass
