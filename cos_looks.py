"""COS outfit pool, matching, camera selection, and prompt construction."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Mapping
from typing import Any, Dict, List, Optional


# Titles use ``·`` for the main split. Matching accepts a full title or a
# complete title segment and never guesses from an arbitrary substring.
COS_LOOK_SETS: List[Dict[str, str]] = [
    {"id": "roxy_cream", "title": "洛琪希·奶油睡衣", "prompt": "严格换装为《无职转生》洛琪希的奶油色居家睡衣两件套：米白奶油色无袖睡衣上衣，柔软微皱棉质，领口与肩线细荷叶边抽褶；胸前白色缎带大蝴蝶结与长飘带；同色宽松睡衣短裤；浅薰衣草紫超长发双麻花辫垂在胸前；姿势严格为跪坐在地毯上，双膝并拢，双腿收拢并拢压在身下，臀部坐在脚跟上，脚部不要向两侧张开，不要盘腿、分腿或站立；对镜坐在木地板地毯上；禁止蓝色旅行法师外套、宽檐帽、法杖。"},
    {"id": "hanfu_peach", "title": "古风·齐胸汉服·桃粉", "prompt": "严格换装为桃粉齐胸汉服：齐胸抹胸高腰，桃粉多层长裙，轻薄裙摆与细微花卉刺绣；高腰翠绿宽丝带和长飘带；黑发高髻配粉色牡丹与金簪；细金项链和长吊坠耳环；古风优雅，日常得体，不要现代礼服。"},
    {"id": "mint_sheer_hanfu", "title": "古风·薄荷粉纱汉服", "prompt": "严格换装为薄荷绿与淡粉渐变的轻纱汉服：外层宽袖薄纱袍，淡雅晕染花卉，内衬浅粉齐胸襦裙；深棕长发高髻、空气刘海，右侧白色绢花，左侧长辫垂腰；细银链小吊坠；清雅自然，日常得体，不要铠甲。"},
    {"id": "rem_blue_lolita", "title": "蕾姆·蓝白女仆洛丽塔", "prompt": "严格换装为《Re:Zero》蕾姆的罗兹瓦尔宅邸经典蓝白女仆服，不是泛化女仆装：浅蓝短发、侧分长刘海，白色褶边女仆发箍与蓝色小花发饰；白色长袖蓬袖衬衣，外穿深蓝至蓝黑色贴身无袖女仆连衣裙/束身上衣，白色褶边胸襟和白色蕾丝围裙覆盖前胸，白色围裙腰带在腰后系大蝴蝶结；深蓝色膝上蓬裙，裙摆白色蕾丝荷叶边，后腰有分叉燕尾式深色后摆；白色过膝袜带深色腿环，黑色圆头玛丽珍女仆鞋。严格保持浅蓝、白、深蓝三色结构，室内柔光对镜全身；不要拉姆粉色女仆服、不要普通黑白法式女仆、不要黑色哥特洛丽塔、不要蓝色水手服、不要校服、不要短袖、不要现代服务员制服。"},
    {"id": "white_slip_mini", "title": "白细肩带迷你裙", "prompt": "严格换装为白细肩带迷你裙：黑发直刘海，高马尾从脑后束起垂到后背。上身纯白细肩带吊带裙，肩带约一指宽，低圆领到锁骨下，无袖，胸腰一体、不束腰封。裙身单层轻薄缎面，自然垂褶，A字微扩，裙摆只到大腿中段。裸腿，白色厚底运动鞋、白鞋带。室内柔光对镜全身。不是拖地白婚纱，也不是齐胸长裙汉服。"},
    {"id": "haiyue_petal_jelly", "title": "海月·白蓝花瓣水母", "prompt": "严格换装为《王者荣耀》海月这一版白蓝花瓣水母短款：深蓝近黑长直发垂过腰。上身近无袖，深甜心领，胸前层层象牙白与浅蓝立体花瓣荷叶，左胸一朵深蓝结晶花，细银链小吊坠垂在腹前；右侧肩上白蓝立体花簇。腰侧大开，露出肋与腰腹。下装高腰多层薄纱裙，髋上圈一圈白蓝紫花瓣荷叶，外层白到浅紫薄纱，左侧高开叉到大腿。主色象牙白、浅蓝、浅紫、少量深蓝。室内柔光对镜全身。不是原皮宽袖长袍汉服。"},
    {"id": "xishi_fan_qipao", "title": "西施·同人短旗袍", "prompt": "严格换装为《王者荣耀》西施同人短旗袍这一版：铂金银白长直发中分，两侧长鬓，头顶一对薄荷白鹿角，一侧薄荷小花发饰。上身是贴身无袖短旗袍，高立领金滚边与金盘扣，领下低开到胸口，正中金绣花或贝壳纹。肩臂另接薄荷薄纱荷叶袖，纱袖垂到小腿。旗袍缎面银白微闪，下摆薄荷暗纹，金边包到大腿中段，一侧高开叉。白色不透明过膝袜，袜口白蕾丝。室内素墙对镜全身。不是原皮长裙水莲汉服。"},
    {"id": "lanmeng_dragon_path", "title": "蓝梦·龙之道", "prompt": "严格换装为《永劫无间》蓝梦龙之道这一版：黑发齐刘海，两侧高发包，发上金珠与金缎带，长段泡泡马尾垂下。上身是浅金海波纹一字肩短上衣，无袖，只到胸下，露出腰腹；胸前菱形黑边开窗，黑滚边与盘扣。外搭松垮黑袍从一侧肩臂垂下。下装是黑色短裹裙，前腰大黑蝴蝶结，粗黄绳在腰间打结并垂到一侧，裙摆到大腿中段。黑珠项链配小金牌。室内柔光对镜全身。不是原皮多层长袍。"},
    {"id": "dolia_ocean_ruffle", "title": "朵莉亚·海洋荷叶裙", "prompt": "严格换装为《王者荣耀》朵莉亚这一版海洋荷叶短裙：青绿蓝长卷发披肩，头顶小蓝发饰。上身细肩带低领短上衣，胸前白贝壳与红海星、细珍珠点缀，颈上贝壳吊坠项圈。腰线收束后接多层荷叶迷你裙，外层薄纱从青绿蓝渐变到蓝紫，内层白色衬裙，裙摆只到大腿，珍珠散在荷叶边上。无袖，裸腿。室内素墙对镜全身。不是原皮人鱼长尾。"},
    {"id": "yinzi_white_qipao", "title": "殷紫萍·银白短旗袍", "prompt": "严格换装为《永劫无间》殷紫萍这一版银白短旗袍：银白长直发齐刘海，两侧高发包，包上白花瓣发饰、黑缎带和白流苏。上身高立领白缎旗袍，领口黑里，前中钥匙孔开窗到上胸，胸前银灰花卉云纹绣，腰侧开窗露腰。短荷叶肩饰，长白纱手套到腕。下装同料迷你裙到大腿中段，黑滚边，内层白荷叶衬裙，白色不透明过膝袜。室内柔光对镜全身。不是长袍旗袍。"},
    {"id": "ganyu_bride", "title": "甘雨·花嫁", "prompt": "严格换装为《原神》甘雨花嫁这一版：淡蓝发高髻配齐刘海和侧缕，保留麒麟角，白蕾丝花冠和薄白头纱。上身白蕾丝立领项圈，珍珠链垂到胸口，中央金铃铛；无袖露肩白胸衣收腰，肩臂接蓬松白到淡蓝薄纱荷叶袖。下装多层白到淡蓝迷你蓬裙，薄纱荷叶很多，一侧更长的淡蓝纱片。白色不透明过膝袜。室内柔光对镜全身。不是原皮深蓝金边旗袍。"},
    {"id": "yulinglong_gold_fox", "title": "玉玲珑·金狐", "prompt": "严格换装为《永劫无间》玉玲珑这一版金狐短装：浅金波浪长发中分，头顶竖起狐耳和细金额饰。上身近无袖，肤色薄纱贴身，胸口大颗青绿宝石和金纹，深色金边项圈，上臂金臂环。腰上金腰带垂几何金饰和蓝绿宝石，露出腰腹。下装香槟金薄纱短裙到大腿，高开叉，内层更浅。室内柔光对镜全身。不是原皮覆盖更多的汉服长袍。"},
    {"id": "diaochan_sanguosha", "title": "貂蝉·三国杀", "prompt": "严格换装为《三国杀》貂蝉这一版：黑长直发，头顶两只螺旋黑发包、金缎缠绕，右侧一朵大金花。颈上金项圈。上身白色短上衣只到胸下，下沿荷叶边，整段腰腹露出。外罩粉红宽袖长纱，袖长过手。下装高腰薄荷缎裙，腰上金饰，左侧高开叉到髋，裙摆拖地，赤足。室内柔光对镜全身。不是齐胸长裙汉服。"},
    {"id": "platinum_lace_gown", "title": "铂金发白蕾丝长裙", "prompt": "严格换装为铂金长直发白蕾丝长裙：头发中分，两缕垂到大腿。上身高领白蕾丝抽褶衣，短蓬袖蕾丝边，腰上同色宽带和扣。下装多层奶白长裙，薄纱外层，一侧用手掀起露出大腿。裸腿。室内素墙对镜全身。不是婚纱，也不是齐胸汉服。"},
    {"id": "xishi_cyan_qipao", "title": "西施·青绿渐变旗袍", "prompt": "严格换装为《王者荣耀》西施这一版青绿渐变旗袍：黑长直发披背。上身高立领，白盘扣，领下到腰是青绿转到象牙白，深棕滚边；右胸白花蝶贴饰；七分袖，袖口白蕾丝，袖缝棕滚边。腰下白内裙微微鼓出。外裙下摆蓝花叶纹，一侧高开叉到大腿，内层白蕾丝衬裙。颈上一串白珠。室内素墙对镜全身。不是原皮长裙水莲，也不是鹿角同人短旗袍。"},
    {"id": "yellow_bow_maid", "title": "黄结白围裙女仆", "prompt": "严格换装为深蓝底白围裙女仆装：黑长直发披背。上身深蓝底衣，白色荷叶水手领黑滚边；胸前大黄蝴蝶结，结心绿宝石。肩上白蓬袖，深蓝袖管，袖口白宽边黑条。腰上白围裙束出荷叶，裙摆白荷叶盖在深蓝裙外。白手套。室内柔光对镜全身。不是黑白法式女仆，也不是蕾姆蓝白女仆。"},
    {"id": "silver_deepv_hanfu", "title": "古风·汉服·银紫深V广袖", "prompt": "严格换装为银紫长直发深V广袖古装：头发中分垂过腰，右侧白花步摇。上身交领极低开到胸口，领边金云纹滚边；前中一条金绣直襟从领口通到裙摆；外层粉白薄纱，腰上浅粉腰带。广袖，肩头藕粉抽褶，袖身粉白薄纱。下装粉白多层长裙，前中金绣直条。室内柔光对镜全身。不是齐胸抹胸汉服。"},
    {"id": "blue_backless_hanfu", "title": "古风·露背蓝纱古装", "prompt": "严格换装为露背蓝纱古装：黑发高髻。构图必须是单人四分之三侧身，身体朝画面一侧转开，镜头同时看到一侧脸颊、一侧锁骨和从颈到腰的整片裸背，不要正面全身，也不要正对镜头的后脑勺。后颈一条亮蓝丝带打结，两根长带沿背沟垂下；后背无交叉带、无第二套肩带。右肩只搭一层浅蓝暗纹薄纱。袖和裙都是多层蓝纱，从冰蓝渐变到青绿再到宝蓝，广袖鼓起但不挡背，长裙拖地。人体只有两条胳膊、两只手，一只手自然垂在身侧，另一只手轻扶裙或纱，不要第三只手、不要重复手臂、不要镜子里再长出一只手。室内黑底柔光，单人侧身对镜。不是齐胸长裙，也不是白婚纱。"},
    {"id": "jixiaoman_black_gold", "title": "姬小满·黑金橙短装", "prompt": "严格换装为《王者荣耀》姬小满这一版黑金橙短装：浅橙粉到珊瑚橙长发披肩。内层白领立领。外层黑色短款宽袖外套只到胸下，金滚边，胸前金纹与金链吊坠，整段腰腹露出。宽袖外黑内亮橙金，袖口金边。腰上金腰带。髋前一块大金六角护甲板，板上有圆环纹。下装黑色短裤，裤口白边，大腿裸出。髋侧一条浅紫白辫状长尾饰。室内柔光对镜全身。不是黄睡衣家居，不是黄短裙，也不是齐胸长裙汉服。"},
    {"id": "xishi_shiyu_jiangnan", "title": "西施·诗语江南", "prompt": "严格换装为《王者荣耀》西施诗语江南这一版青绿短款：黑长发披肩，一侧编小辫，金花叶发饰。上身贴身青绿短衣，白花绣，金滚边；高立领金边；左肩一团青绿荷叶大结；内衬白底白花金边，前襟掀起露出腰腹。广袖，外层青绿金袖口，内层白袖金边。腰侧粉红流苏小囊。下装浅青绿白多层迷你蓬裙，裙摆只到大腿。白色不透明过膝袜，袜口宽米色边。室内素墙对镜全身。不是原皮长裙水莲，不是鹿角同人短旗袍，也不是青绿长旗袍。"},
    {"id": "gongsunli_lihenyan", "title": "公孙离·离恨烟", "prompt": "严格换装为《王者荣耀》公孙离离恨烟这一版：深蓝近黑长发，头顶两只尖角高发包，包上红珠和金簪，金流苏垂侧。上身贴身白缎短旗袍，高立白领金绣，胸口正中大金螺旋圆徽；一侧长白袖金边，另一侧露肩，上臂金环。腰上金蓝绳结，垂金环、流苏和金葫芦。下装极短白底，两侧高开到髋，外搭青绿、粉、深蓝多层曳地薄纱。赤足。室内柔光对镜全身。不是大乔，不是普通白旗袍，也不是西施青绿旗袍。"},
    {"id": "xishi_crop_qipao", "title": "西施·露腰短旗袍", "prompt": "严格换装为《王者荣耀》西施这一版露腰短旗袍两件套：黑长直发披肩。上身高立领青绿缎，白盘扣/蓝花结盘扣，深棕滚边，白花叶绣，只到胸下，整段腰腹露出；七分袖，袖口白蕾丝，袖身花纹棕滚边。下装高腰白荷叶短裙盖在青绿花纹迷你裙上，白蕾丝裙边，裙摆只到大腿上段。白色不透明过膝袜，袜口白蕾丝花边。室内素墙对镜全身。不是诗语江南广袖短衣，也不是鹿角同人短旗袍，也不是领下接到腰的青绿长旗袍。"},
    {"id": "ying_black_red", "title": "影·黑白红短装", "prompt": "严格换装为《王者荣耀》影这一版黑白红短装：铂金银白长直发披肩，右侧一束鲜红流苏发饰。上身黑立领短衣，银纹护领；胸口正中大红圆宝石，宝石下大红V形开窗到胸，整段腰腹露出。袖不对称：持械那侧白纱垂袖，另一侧黑垂片。下装贴身亮黑短裤，髋侧垂白色长片。右手红环刃陀螺。室内柔光对镜全身。不是齐胸汉服，也不是全包黑袍。"},
    {"id": "lusha_gold_tiara", "title": "露莎·白金短装", "prompt": "严格换装为铂金超长直发白金短装：头发拖地，齐刘海。头顶金冠，正中紫宝石，两侧金饰垂紫金缎带和细金链。上身无袖白抹胸，金交叉胸带，下沿金链；上臂金环。腰上宽金腰带，垂金片和流苏，腰腹露出。下装白短裙，两侧高开到大腿，后摆更长。金绑带高跟凉鞋，脚面紫结。室内柔光对镜全身。不是拖地白婚纱，也不是齐胸长裙汉服。"},
    {"id": "yangyang_blue_floral_swim", "title": "秧秧·蓝白碎花泳装", "prompt": "严格换装为《鸣潮》秧秧这一版蓝白碎花泳装：黑长直发，发尾染蓝，左侧银发卡。上身细吊带白底蓝花比基尼，蓝荷叶滚边，胸前蓝结。无袖，整段腰腹露出。下装同料白底蓝花薄纱裹裙，左髋打结，一侧高开。室内柔光对镜全身。不是原皮长外套白裙，也不是普通白婚纱。"},
    {"id": "cartethyia_black_bird", "title": "卡提希娅·黑裙飞鸟", "prompt": "严格换装为《鸣潮》卡提希娅这一版：浅冰蓝长发。颈上黑立领，正中金翼剑徽。上身白胸衣，蓝藤花绣，外罩黑色金环胸带。肩上白到浅蓝短披，肩头圆环纹。左上臂藕粉袖箍金环。下装黑色迷你裙，裙上白绣飞鸟，一侧蓝带和金链。赤足，银枝状脚环。室内柔光对镜全身。不是白婚纱，也不是齐胸汉服。"},
    {"id": "yuno_gold_pink_armor", "title": "尤诺·金粉轻甲", "prompt": "严格换装为《鸣潮》尤诺这一版金粉轻甲 COS：银灰到浅灰长发，发饰带金属与柔软毛绒质感。上身香槟金光泽胸甲与肩部护具，中央和肩侧有圆环金属结构，内层是浅粉色衣料；白色轻纱长袖与长披帛从肩侧垂下，腰间搭配金色圆环、细链、徽章、流苏和垂坠装饰，局部点缀蓝绿色细节。室内暗色背景、粉色沙发或软垫，暖色侧光突出金属反光和布料褶皱，竖屏手机近景或半身摄影，重点展示服装层次、配件和材质。不是普通白裙，不是现代战术铠甲，不要字幕、水印或马赛克。"},
    {"id": "mint_twin_braid_hanfu", "title": "古风·青绿双辫广袖长裙", "prompt": "严格换装为深棕双麻花辫青绿广袖长裙：两股粗辫垂在胸前。上身淡青绿立领短衣，领前白花绣，内层白襟。广袖淡青绿，褐花藤绣，袖里白。腰侧粉红花囊。下装白到淡青绿多层蓬长裙。室内素墙对镜全身。不是齐胸抹胸汉服，也不是迷你短裙。"},
    {"id": "mansui_gray_wafu", "title": "满穗·灰白和风", "prompt": "严格换装为满穗风格的灰白色和风 COS 造型：黑色长发齐刘海，双侧丸子头，白色大蝴蝶结发饰和白色飘带。上身宽松灰白色长袖上衣，柔软布料与自然褶皱，领口和胸前有深色细滚边；下装深灰色高腰长裙，多层自然垂落的褶皱裙摆，裙摆略带轻薄质感；自然裸腿，搭配简洁黑色平底鞋。单人竖屏手机高机位俯拍，人物跪坐或半跪在室外地面，抬头看向镜头，一只手在脸旁比 V，另一只手轻轻整理裙摆。背景是旧墙、灰色地面和少量散落树叶，阴天自然光，真实手机摄影质感，画面干净得体。不要中筒袜、短袜或厚重打底袜，不要字幕、贴纸、水印或额外人物。"},
    {"id": "xiao_qiao_white_bear", "title": "小乔·白熊围巾", "prompt": "严格换装为薄荷绿白熊主题的完整 COS 造型：薄荷绿色长发或中长卷发，头顶白色圆形熊耳发饰，发间有少量彩色小装饰。上身白色宽松蓬袖上衣，袖口和肩部带薄荷绿色装饰；颈部围着粗针织黄色长围巾，围巾两端自然垂下；腰间有粉色蝴蝶结。下装亮蓝色和青绿色百褶短裙，裙面有白色和金黄色几何线条，裙前悬挂小人偶、星星和流苏装饰。腿部穿白色不透明过膝袜或连贯白色腿部服装，从大腿上方连续到脚踝，不要中筒袜、短袜或袜口截断；脚穿白色与米黄色动物毛绒拖鞋，带蓝色和粉色绑带装饰。单人竖屏手机正面全身摄影，双手自然抬起整理黄色围巾，明亮普通房间光线，真实真人 COS 摄影质感。不要视频字幕、文字、水印、贴纸或第二个人。"},
    {"id": "furina_cream_blue_ruffle", "title": "芙宁娜·奶油浅蓝荷叶裙", "prompt": "严格换装为芙宁娜风格的奶油浅蓝荷叶裙 COS 造型：白金色短卷发或齐肩波浪发，发丝中混合浅蓝色挑染，前额柔软碎发和两缕侧边卷发，头顶佩戴浅色猫耳发饰。上身奶油米白色荷叶边上衣，肩部和胸前有多层柔软荷叶边，长袖略宽松，面料带浅粉色和浅蓝色小花图案，胸前佩戴细链项链。腰部系灰蓝色大蝴蝶结。下装浅蓝灰色多层蓬松半身裙，由多层蓝灰色布料和奶油白荷叶边组成，每层边缘带细白色蕾丝，裙面有少量细小花朵装饰。单人竖屏手机正面近景或三分之二身构图，人物正对镜头，表情温柔自然，双手自然放在身体两侧或轻轻展开裙摆，画面从头顶拍到膝盖附近。室内温暖客厅背景，木色柜体和柔和环境光，真实真人 COS 摄影质感。不要字幕、猫咪贴纸、文字、水印、塑料皮肤、过度磨皮或额外人物。"},
    {"id": "ancient_hanfu_halter_dudou", "title": "古风·汉服挂脖肚兜套装", "prompt": "严格换装为白色与淡紫色国风汉服挂脖肚兜套装：细肩带在颈后交叉，胸前有白色花卉蕾丝刺绣与几何镂空，前襟垂落半透明轻纱和中央黑色流苏；外搭淡紫色薄纱披肩或袖套；下装为白色与浅紫色短裙，面料轻盈，边缘带柔软褶皱。米白色沙发，室内柔光，人物自然坐姿，一腿屈起，一手轻拨长发，另一手支撑身体；竖屏商品展示或日常穿搭摄影，重点展示服装结构、蕾丝、纱料和垂感。保持自然遮挡与得体构图，不要现代内衣、水印、字幕或额外人物。"},
    {"id": "xiaowu_pink_rabbit", "title": "小舞·浅粉白兔", "prompt": "严格换装为《斗罗大陆》小舞风格的浅粉白兔系 COS：深棕色超长直发，白色半透明蓬袖，浅粉色露腰短上衣与胸衣，胸前银白镂空花纹饰片和中央粉色宝石，腰间灰银色花饰束带，浅粉色多层荷叶短裙、白色蕾丝边和少量淡紫内层。室内柔光，人物斜躺或坐靠在米白与浅粉床品上，竖屏近景带到腰线或大腿，手轻整理胸前衣料或裙摆，真实真人 COS 摄影，服装结构清晰。不要现代睡衣、额外人物、字幕或水印。"},
    {"id": "wangzhaojun_old_blue", "title": "王昭君·旧版原皮蓝白", "prompt": "严格换装为《王者荣耀》王昭君旧版原皮 COS：冰蓝色长直假发，两侧长发垂落，蓝色蝴蝶结发饰，白色毛绒披肩与毛边袖口，亮蓝色缎面上衣和短裙，胸前银蓝几何装饰，蓝白分层与少量透明纱料，搭配银蓝色小配件。坐靠粉色床面，镜头从上方近距离拍摄胸腰到大腿，柔和室内光，真实手机 COS 摄影质感，突出蓝色缎面、白色毛绒和银色配饰。不要普通蓝裙、现代外套、字幕或水印。"},
    {"id": "yulinglong_red_thread", "title": "玉玲珑·喜缘红线", "prompt": "严格换装为《永劫无间》玉玲珑“喜缘红线”造型：黑色盘发与两侧发髻，红金珠宝头冠，白粉色绒球耳饰，红色发饰与金色流苏；红金凤凰纹裹胸短上衣，半透明白色蓬袖，红色颈饰和多层金色胸饰，深红色腰封，多层红色薄纱裙片，金色链饰与白色绒球流苏。人物一手扶腰，另一手持展开的粉红色折扇，红色帷幕后暖红舞台光，竖屏三分之二身环境人像，喜庆国风表演氛围。不要字幕、水印或额外人物。"},
    {"id": "anime_girl_orange_mint", "title": "二次元少女·橙青半透", "prompt": "严格换装为橙金与薄荷青配色的二次元少女 COS：橙金色轻纱短裙或裙摆层，薄荷青短袖外搭，白色半透明长袖，浅蓝色滚边，宽蝴蝶结或腰带，轻薄有垂感的多层布料。人物侧坐粉色扶手椅，一条腿自然屈起抬高，横向近景拍摄腰部到膝部，背景是粉色房间、床铺和一排毛绒玩偶，室内柔光，真实手机视频质感，服装褶皱清楚，画面干净。不要额外人物、字幕或水印。"},
    {"id": "yao_mint_blue_dress", "title": "瑶·薄荷冰蓝短裙", "prompt": "严格换装为《王者荣耀》瑶风格的薄荷冰蓝梦幻短裙 COS：薄荷绿色双马尾，浅蓝色亮泽短裙，白色半透明蓬袖，冰蓝色多层荷叶纱裙，青绿色大蝴蝶结，腰前白色小狗毛绒挂件与白色绒球，裙边白色蕾丝。人物坐在木地板上，双腿自然屈起，俯拍竖屏近景，双手轻整理裙摆，室内自然光，真实手机视频质感，突出透明纱料、荷叶边和毛绒配件。不要模糊脸、字幕、水印或额外人物。"},
    {"id": "luna_zixia_fairy", "title": "露娜·紫霞仙子", "prompt": "严格换装为《王者荣耀》露娜“紫霞仙子” COS：黑色长直发，白色与淡紫色交领短上衣，胸前淡紫色半透明交叉层叠，领口与腰线深蓝色滚边和金色包边，白紫色宽袖或披肩，肩部金色蝴蝶形护饰与小铃铛，金色腰带、几何金属腰饰，淡紫色褶边短裙或侧摆。低光室内或夜间场景，人物身体略侧转，镜头近距离取胸口到腰线，暖黄色侧光，真实手机视频摄影质感，主体居中。不要黑色上下边框、字幕或水印。"},
    {"id": "ancient_blue_floral_halter_doudou", "title": "古风·浅蓝花卉挂脖兜兜", "prompt": "严格换装为轻国风浅蓝花卉挂脖兜兜套装：黑色长发，双侧低髻或丸子发髻，长发自然垂落胸前；内穿白色、淡紫与浅蓝渐变的挂脖兜兜上衣，颈后系带，领口与胸前使用浅蓝色包边，胸口中央有圆形镂空和金色古典扣饰，衣身带白色花卉暗纹刺绣与轻微浮雕质感；外搭浅蓝色半透明薄纱短衫，宽松长袖，袖口自然堆叠，前襟排列白色珍珠纽扣；下装为米白色或浅灰色多层轻纱褶皱长裙，腰部自然收拢。明亮素白背景，室内柔和高光，竖屏手机穿搭摄影，正面近距离胸腰构图，重点展示挂脖领口、圆形镂空、金色扣饰、花卉刺绣、薄纱袖和珍珠纽扣；一手轻抬靠近胸前或拨弄长发，另一手自然垂落，画面清透自然。不要现代内衣、塑料材质、水印、字幕或额外人物。"},
    {"id": "kamisato_ayaka_white_blue_hakama", "title": "神里绫华·白袖蓝袴", "prompt": "严格换装为《原神》神里绫华经典 COS 造型：冰蓝色长发，齐刘海，高马尾，侧边长发自然垂至胸前，佩戴深蓝黑色发饰与金色装饰，双侧佩戴粉色水引结蝴蝶结耳饰，紫蓝色眼睛。上身为白色交领和服式短上衣，浅蓝色内领，宽大的白色长袖，衣襟与袖口有细致层叠结构；腰间系深蓝色宽腰封，中央有粉色蝴蝶结和长流苏；下装为深海军蓝色褶裙或袴裙，裙摆厚重垂坠，前方有开衩结构，露出自然腿部线条。室内柔光，竖屏手机动态 COS 摄影，中景到膝上构图；人物可正面站立、轻坐或微微转身，一手整理衣襟，另一手轻轻展开宽袖或扶住裙摆，动作自然，轻微动态模糊，二次元妆面与真人 COS 质感结合。不要武器、额外人物、现代服装、字幕或水印。"},
]

COS_LOOK_CATEGORY_TERMS = (
    "旗袍", "汉服", "女仆", "睡衣", "长裙", "短裙", "短装", "泳装", "古装", "古风", "肚兜", "挂脖",
    "洛丽塔", "花嫁", "围裙", "白熊", "和风", "荷叶裙", "兜兜", "袴裙",
)


def _cos_item_terms(item: Mapping[str, Any]) -> List[str]:
    title = str(item.get("title") or "")
    compact_title = re.sub(r"\s+", "", title).lower()
    terms: List[str] = [compact_title]
    for part in re.split(r"[·/\s、，,：:（）()]+", title):
        part = part.strip().lower()
        if len(part) >= 2 or (len(part) == 1 and "\u4e00" <= part <= "\u9fff"):
            terms.append(part)
    for term in COS_LOOK_CATEGORY_TERMS:
        if term in title:
            terms.append(term.lower())
    return list(
        dict.fromkeys(
            term
            for term in terms
            if len(term) >= 2
            or (len(term) == 1 and "\u4e00" <= term <= "\u9fff")
        )
    )


def match_cos_look_sets(text: str) -> List[Dict[str, str]]:
    """Match by full title, character, or outfit category."""
    raw_query = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not raw_query:
        return []
    all_items = [dict(item) for item in COS_LOOK_SETS]
    separators = r"[\s·/／、，,：:（）()\[\]【】;；。.!！？?]+"

    def compact(value: str) -> str:
        return re.sub(separators, "", str(value or "")).lower()

    item_terms: Dict[str, set[str]] = {}
    term_items: Dict[str, set[str]] = {}
    category_term_set = {compact(term) for term in COS_LOOK_CATEGORY_TERMS}
    for item in all_items:
        item_id = str(item.get("id") or "")
        terms = {compact(term) for term in _cos_item_terms(item)}
        terms.discard("")
        item_terms[item_id] = terms
        for term in terms:
            term_items.setdefault(term, set()).add(item_id)
    known_terms = sorted(term_items, key=lambda term: (-len(term), term))

    def segment_token(token: str) -> List[str]:
        value = compact(token)
        if not value:
            return []
        best: List[Optional[List[str]]] = [None] * (len(value) + 1)
        best[0] = []
        for start in range(len(value)):
            if best[start] is None:
                continue
            for term in known_terms:
                if value.startswith(term, start):
                    end = start + len(term)
                    candidate = [*best[start], term]
                    previous = best[end]
                    if previous is None or len(candidate) < len(previous):
                        best[end] = candidate
        return best[-1] or []

    compact_query = compact(raw_query)
    exact = [
        dict(item)
        for item in all_items
        if compact(str(item.get("title") or "")) == compact_query
    ]
    if exact:
        return exact

    selected_name_terms: List[str] = []
    selected_category_terms: List[str] = []
    for token in re.split(separators, raw_query):
        segments = segment_token(token)
        if not segments:
            continue
        for term in segments:
            if term in category_term_set:
                selected_category_terms.append(term)
            else:
                selected_name_terms.append(term)
    selected_name_terms = list(dict.fromkeys(selected_name_terms))
    selected_category_terms = list(dict.fromkeys(selected_category_terms))
    if selected_name_terms or selected_category_terms:
        pool = []
        for item in all_items:
            terms = item_terms.get(str(item.get("id") or ""), set())
            if all(term in terms for term in selected_name_terms) and all(
                category in terms for category in selected_category_terms
            ):
                pool.append(dict(item))
        return pool
    return []


def pick_cos_look_set(*, avoid_id: str = "", query: str = "") -> Dict[str, str]:
    matched = match_cos_look_sets(query)
    pool = matched or list(COS_LOOK_SETS)
    pool = [item for item in pool if str(item.get("id") or "") != str(avoid_id or "")]
    if not pool:
        pool = matched or list(COS_LOOK_SETS)
    return dict(random.choice(pool))


def format_cos_look_list() -> str:
    lines = [f"看看COS 随机池（{len(COS_LOOK_SETS)}套）："]
    lines.extend(
        f"{index}. {item.get('title') or '未命名套装'}"
        for index, item in enumerate(COS_LOOK_SETS, 1)
    )
    return "\n".join(lines)


def list_cos_look_sets() -> List[Dict[str, str]]:
    """Return validated copies for dashboard and quick-test pickers."""
    return [
        {
            "id": str(item.get("id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "prompt": str(item.get("prompt") or "").strip(),
        }
        for item in COS_LOOK_SETS
        if str(item.get("id") or "").strip()
        and str(item.get("title") or "").strip()
        and str(item.get("prompt") or "").strip()
    ]


def parse_requested_cos_camera(text: str) -> str:
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw).lower()
    blob = compact + raw.lower()
    third_keys = (
        "他拍", "别人拍", "别人帮拍", "朋友拍", "有人拍", "被拍", "抓拍",
        "第三人称", "路人视角", "摄影师拍", "不是自拍", "非自拍", "不要自拍",
        "不要对镜", "不拿手机", "不要拿手机", "不要手持手机", "candid",
        "thirdperson", "notselfie",
    )
    selfie_keys = ("自拍", "对镜", "镜前", "镜子前", "自己拍", "selfie", "mirror")
    if any(key in blob for key in third_keys):
        return "third"
    if any(key in blob for key in selfie_keys):
        return "selfie"
    return ""


def pick_cos_camera(*, extra_request: str = "", avoid: str = "", camera: str = "") -> str:
    forced = str(camera or "").strip() or parse_requested_cos_camera(extra_request)
    if forced in {"selfie", "third"}:
        return forced
    pool = ["selfie", "third"]
    if avoid in pool:
        pool = [item for item in pool if item != avoid] or pool
    return random.choice(pool)


def adapt_cos_outfit_for_camera(outfit: str, camera: str) -> str:
    text = str(outfit or "")
    if camera != "third":
        return text
    replacements = (
        ("对镜坐在木地板地毯上", "坐在木地板地毯上"),
        ("室内柔光对镜全身", "室内柔光半身"),
        ("室内素墙对镜全身", "室内素墙半身"),
        ("室内黑底柔光，单人侧身对镜", "室内黑底柔光，单人侧身半身"),
        ("对镜全身", "对镜半身"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text.replace("对镜", "")


def build_cos_look_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_id: str = "",
    avoid_camera: str = "",
    camera: str = "",
    match_query: str = "",
    picker: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> str:
    """Build one matched or random COS action with a compatible camera."""
    choose = picker or pick_cos_look_set
    chosen = choose(
        avoid_id=avoid_id,
        query=str(match_query).strip() if str(match_query).strip() else extra_request,
    )
    title = str(chosen.get("title") or "随机COS")
    camera_kind = pick_cos_camera(
        extra_request=extra_request, avoid=avoid_camera, camera=camera
    )
    outfit = adapt_cos_outfit_for_camera(
        str(chosen.get("prompt") or "").strip(), camera_kind
    )
    cos_id = str(chosen.get("id") or "cos")
    if camera_kind == "third":
        framing = (
            "【他拍 / 看看COS模式】"
            "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
            "别人视角的单人成品照：竖屏手机近景半身或环境人像，像随手拍的 COS 封面；画面里只有主角一个人；"
            "拍摄者完全在画面外，不要第二个人，不要有人举着手机拍主角，不要拍到拍照过程；"
            "不要对镜、不要镜子、不要手持手机入镜，不要第一人称伸手自拍，不要手臂挡脸挡衣服。"
        )
    else:
        framing = (
            "【自拍 / 看看COS模式】"
            "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
            "竖屏手机近景半身自拍：可对镜，但拍胸像到腰线，不要展会式全身棚拍；手机可出现在镜中；"
            "不要第一人称伸手自拍，不要手臂挡脸挡衣服。"
        )
    base = (
        framing
        + "脸型五官必须保持形象参考，不要换成别人的脸；"
        + "假发颜色/发型/发饰可按本套 COS 完整替换。"
        + f"本次套装：{title}。"
        + outfit
        + "服装颜色、层数、配饰、开叉、荷叶边、鞋履等结构要尽量齐全高还原；"
        + "构图完整带上腰线；竖屏近景半身即可，不要简化成普通常服；画面干净得体。"
    )
    if has_refs:
        base = "参考用户附图的氛围或构图，" + base
    extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
    if extra and extra not in base:
        base += f" 用户补充要求优先：{extra}。"
    return base + f" 【cos:{cos_id}】 【cam:{camera_kind}】"
