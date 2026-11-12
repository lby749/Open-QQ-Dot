import json

import share_links


def test_extracts_bilibili_json_share():
    payload = json.dumps({
        "app": "com.tencent.structmsg",
        "meta": {"news": {"title": "测试视频", "desc": "B站分享",
                          "jumpUrl": "https://b23.tv/BV1GJ411x7h7",
                          "preview": "https://i0.hdslb.com/cover.jpg"}}},
        ensure_ascii=False)
    event = {"message": [{"type": "json", "data": {"data": payload}}]}
    shares = share_links.extract_from_event(event)
    assert len(shares) == 1
    assert shares[0]["platform"] == "bilibili"
    assert shares[0]["url"] == "https://b23.tv/BV1GJ411x7h7"
    assert "B站分享" in share_links.text_for(shares)


def test_extracts_douyin_xml_share():
    xml = ('<msg><item><title>抖音视频</title>'
           '<url>https://v.douyin.com/abc123/</url></item></msg>')
    event = {"message": [{"type": "xml", "data": {"data": xml}}]}
    shares = share_links.extract_from_event(event)
    assert len(shares) == 1
    assert shares[0]["platform"] == "douyin"
    assert "抖音分享" in share_links.text_for(shares)


def test_ignores_other_links_and_deduplicates():
    event = {"message": [
        {"type": "text", "data": {"text": "看这个 https://www.bilibili.com/video/BV1xx 和 https://example.com/x"}},
        {"type": "text", "data": {"text": "https://www.bilibili.com/video/BV1xx"}},
    ]}
    shares = share_links.extract_from_event(event)
    assert len(shares) == 1
    assert shares[0]["url"] == "https://www.bilibili.com/video/BV1xx"


def test_message_parts_adds_share_text():
    import main
    payload = json.dumps({"meta": {"news": {"title": "分享标题",
                                           "jumpUrl": "https://b23.tv/BV1GJ411x7h7"}}},
                         ensure_ascii=False)
    body, _ = main.message_parts({"message": [{"type": "json", "data": {"data": payload}}]})
    assert "B站分享" in body
    assert "https://b23.tv/BV1GJ411x7h7" in body
