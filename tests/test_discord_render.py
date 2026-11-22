import unittest

import helpers  # noqa: F401

from web.discord_render import markdown


class SegmentTest(unittest.TestCase):
    def test_구분_기호가_있는_줄은_항목마다_나눈다(self):
        self.assertEqual(
            markdown('**113**게임 | 승률 **18%**'),
            '<div class="dc-line dc-segs"><span><strong>113</strong>게임</span><span>승률 <strong>18%</strong></span></div>',
        )

    def test_작은_글자_줄도_나눈다(self):
        self.assertEqual(
            markdown('-# 최근 20게임 | RP +414'),
            '<div class="dc-small dc-segs"><span>최근 20게임</span><span>RP +414</span></div>',
        )

    def test_강조가_구분_기호를_넘으면_나누지_않는다(self):
        self.assertEqual(markdown('**가 | 나**'), '<div class="dc-line"><strong>가 | 나</strong></div>')
        self.assertEqual(markdown('`가 | 나`'), '<div class="dc-line"><code>가 | 나</code></div>')

    def test_구분_기호가_없는_줄은_그대로_둔다(self):
        self.assertEqual(markdown('가|나'), '<div class="dc-line">가|나</div>')
        self.assertEqual(markdown(''), '<div class="dc-line">&nbsp;</div>')


if __name__ == '__main__':
    unittest.main()
