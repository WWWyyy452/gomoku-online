from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import os
import math
import copy

random.seed(os.urandom(32))
import time
import eventlet
from ai_chat import get_commentary

app = Flask(__name__)
app.config["SECRET_KEY"] = str(random.randint(10**9, 10**10))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

GRID = 15
rooms = {}  # room_id -> {...}
_room_seq = 10000  # 自增房间号，永不重复


def gen_room_id():
    global _room_seq
    _room_seq += 1
    return str(_room_seq)


def new_board():
    return [[0] * GRID for _ in range(GRID)]


def check_win(board, r, c, p):
    for dr, dc in [(0, 1), (1, 0), (1, 1), (1, -1)]:
        cnt = 1
        for s in (1, -1):
            nr, nc = r + dr * s, c + dc * s
            while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == p:
                cnt += 1
                nr += dr * s
                nc += dc * s
        if cnt >= 5:
            return True
    return False


def find_win_cells(board, p):
    for r in range(GRID):
        for c in range(GRID):
            if board[r][c] != p:
                continue
            for dr, dc in [(0, 1), (1, 0), (1, 1), (1, -1)]:
                cells = [(r, c)]
                for s in (1, -1):
                    nr, nc = r + dr * s, c + dc * s
                    while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == p:
                        cells.append((nr, nc))
                        nr += dr * s
                        nc += dc * s
                if len(cells) >= 5:
                    return cells
    return []


def is_full(board):
    return all(board[i][j] for i in range(GRID) for j in range(GRID))


# ═══ 高性能五子棋 AI（共享棋盘 + 3层搜索 + 双威胁检测）═══
DIRECTIONS = [(0, 1), (1, 0), (1, 1), (1, -1)]

# 棋型评分（平衡攻防）
SCORE_TABLE = {
    "five": 100000000,  # 五连 - 最高优先级
    "open_four": 10000000,  # 活四 - 必赢/必防
    "four": 1000000,  # 冲四 - 极高威胁
    "open_three": 200000,  # 活三 - 极高威胁（提高）
    "three": 20000,  # 眠三（提高）
    "open_two": 2000,  # 活二（提高）
    "two": 200,  # 眠二（提高）
    "one": 10,  # 单子
}
DEFEND_MULT = 1.2  # 防守系数（稍微偏向防守）

# 双威胁加成
DOUBLE_THREAT_BONUS = 500000  # 双活三/四三组合额外奖励（提高）
THREAT_COUNT_BONUS = 100000  # 威胁数量奖励（提高）

# 连接性奖励 - 鼓励形成多点连接
CONNECTIVITY_BONUS = 100  # 每个相邻棋子奖励


def _count_line(board, r, c, dr, dc, player):
    """沿某个方向数连续同色棋子数（不含起始点）"""
    cnt = 0
    nr, nc = r + dr, c + dc
    while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
        cnt += 1
        nr += dr
        nc += dc
    return cnt


def _line_open(board, r, c, dr, dc, player):
    """检查方向末端是否为空（可延伸）"""
    nr, nc = r + dr, c + dc
    while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
        nr += dr
        nc += dc
    if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == 0:
        # 检查再下一步是否为空（活的定义：两端都至少有一个空位）
        return True
    return False


def _line_pattern(board, r, c, dr, dc, player):
    """分析一个方向的棋型，返回 (连续数, 是否活)"""
    cnt = 1
    open_ends = 0
    for s in (1, -1):
        nr, nc = r + dr * s, c + dc * s
        found = 0
        while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
            found += 1
            nr += dr * s
            nc += dc * s
        cnt += found
        # 末端为空且不在边界外
        if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == 0:
            # 检查这个空位的再远端是否有同色（避免把死棋当活棋）
            open_ends += 1
    return cnt, open_ends


def _score_pattern(total, opens):
    """根据连续数和开口数计算棋型得分"""
    if total >= 5:
        return SCORE_TABLE["five"]
    elif total == 4:
        if opens >= 2:
            return SCORE_TABLE["open_four"]
        elif opens == 1:
            return SCORE_TABLE["four"]
    elif total == 3:
        if opens >= 2:
            return SCORE_TABLE["open_three"]
        elif opens == 1:
            return SCORE_TABLE["three"]
    elif total == 2:
        if opens >= 2:
            return SCORE_TABLE["open_two"]
        elif opens == 1:
            return SCORE_TABLE["two"]
    elif total == 1 and opens >= 1:
        return SCORE_TABLE["one"]
    return 0


def _count_threats(board, r, c, player):
    """统计落子后产生的威胁数量（活三、冲四、活四）"""
    threes = 0
    fours = 0
    for dr, dc in DIRECTIONS:
        total, opens = _line_pattern(board, r, c, dr, dc, player)
        if total >= 5:
            return 99, 99  # 直接赢
        elif total == 4:
            fours += 1
        elif total == 3 and opens >= 2:
            threes += 1
    return threes, fours


def _get_threat_type_global(board, r, c, player):
    """判断(r,c)放player后形成的威胁类型（全局函数版本）"""
    fours = 0
    open_fours = 0
    for dr, dc in DIRECTIONS:
        total, opens = _line_pattern(board, r, c, dr, dc, player)
        if total >= 5:
            return "five"
        elif total == 4:
            if opens >= 2:
                open_fours += 1
            elif opens == 1:
                fours += 1
    if open_fours >= 1:
        return "open_four"
    if fours >= 1:
        return "four"
    return "none"


def _detect_double_threat(board, r, c, player):
    """检测落子是否创造双威胁（双活三 / 四三 / 双冲四）
    返回额外加成分"""
    board[r][c] = player
    threes, fours = _count_threats(board, r, c, player)
    board[r][c] = 0

    # 双活三
    if threes >= 2:
        return DOUBLE_THREAT_BONUS
    # 四三组合
    if fours >= 1 and threes >= 1:
        return DOUBLE_THREAT_BONUS * 1.2
    # 单个威胁也有少量奖励
    if threes >= 1 or fours >= 1:
        return THREAT_COUNT_BONUS * (threes + fours)
    return 0


def evaluate_move(board, r, c, player):
    """评估在(r,c)放player棋子的综合得分（重进攻+双威胁+挡二成二）"""
    opponent = 3 - player
    attack_score = 0
    defend_score = 0
    block_bonus = 0
    
    for dr, dc in DIRECTIONS:
        # 进攻
        total, opens = _line_pattern(board, r, c, dr, dc, player)
        attack_score += _score_pattern(total, opens)
        
        # 防守：模拟这个位置放对手棋子的威胁
        total_d, opens_d = _line_pattern(board, r, c, dr, dc, opponent)
        defend_score += _score_pattern(total_d, opens_d)
        
        # 挡二成二奖励：如果这个位置能挡住对手的活二，同时形成自己的活二
        if total_d == 2 and opens_d >= 2:  # 对手这里有活二
            if total == 2 and opens >= 2:  # 我们这里有活二
                block_bonus += 5000  # 挡二成二奖励
            else:
                block_bonus += 500  # 单纯挡二奖励
    
    # 双威胁加成（进攻性极强）
    threat_bonus = _detect_double_threat(board, r, c, player)
    
    # 连接性奖励：与已有棋子相邻越多越好
    conn_bonus = 0
    for dr, dc in DIRECTIONS:
        for s in (1, -1):
            nr, nc = r + dr * s, c + dc * s
            if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                conn_bonus += CONNECTIVITY_BONUS
    
    # 位置分（越靠近中心越好）
    center = GRID // 2
    dist = abs(r - center) + abs(c - center)
    pos_score = max(0, 14 - dist)
    
    return (
        attack_score * 1.5
        + defend_score * DEFEND_MULT
        + threat_bonus
        + block_bonus
        + conn_bonus
        + pos_score
    )


def get_candidates(board):
    """获取候选落子位置（已有棋子周围2格内的空位）"""
    candidates = set()
    has_stone = False
    for r in range(GRID):
        for c in range(GRID):
            if board[r][c] != 0:
                has_stone = True
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == 0:
                            candidates.add((nr, nc))
    if not has_stone:
        return [(GRID // 2, GRID // 2)]
    return list(candidates) if candidates else [(GRID // 2, GRID // 2)]


def find_immediate_win(board, player):
    """找必胜点"""
    for r, c in get_candidates(board):
        board[r][c] = player
        if check_win(board, r, c, player):
            board[r][c] = 0
            return (r, c)
        board[r][c] = 0
    return None


def find_immediate_block(board, my_player):
    """找必须防守的点（对手的必胜点）- 增强版"""
    opponent = 3 - my_player
    
    # 1. 检查对手是否能一步赢（五连）
    win_block = find_immediate_win(board, opponent)
    if win_block:
        return win_block
    
    # 2. 检查对手当前是否有四连（冲四/活四/跳四）- 必须立即防守
    # 这是最高优先级！
    block_four = _find_existing_four(board, opponent)
    if block_four:
        return block_four
    
    # 2.5 额外检查：扫描所有可能的四子连线（包括跳四）
    jump_four_block = _find_jump_four(board, opponent)
    if jump_four_block:
        return jump_four_block
    
    # 3. 检查对手落子后是否形成活四（两端都开放的四连）- 必须防守
    for r, c in get_candidates(board):
        board[r][c] = opponent
        threat_type = _get_threat_type_global(board, r, c, opponent)
        board[r][c] = 0
        if threat_type == "open_four":
            return (r, c)
    
    # 4. 检查对手落子后是否形成冲四 - 必须防守
    # 这是关键：对手一个冲四，下一步就能赢
    for r, c in get_candidates(board):
        board[r][c] = opponent
        threat_type = _get_threat_type_global(board, r, c, opponent)
        board[r][c] = 0
        if threat_type == "four":
            return (r, c)
    
    # 5. 检查对手是否能形成四连（三连+可延伸）- 必须防守
    # 这是预防性防守：在对手形成四连之前就防守
    for r, c in get_candidates(board):
        board[r][c] = opponent
        # 检查是否形成四连（不是活四或冲四，而是简单的四连）
        has_four = False
        for dr, dc in DIRECTIONS:
            total, opens = _line_pattern(board, r, c, dr, dc, opponent)
            if total == 4:
                has_four = True
                break
        board[r][c] = 0
        if has_four:
            return (r, c)
    
    # 6. 检查对手当前是否有活三 - 必须防守（防止对手形成活四）
    best_three_block = None
    best_three_score = 0
    for r, c in get_candidates(board):
        board[r][c] = opponent
        # 检查是否形成活三
        is_open_three = False
        three_count = 0
        for dr, dc in DIRECTIONS:
            total, opens = _line_pattern(board, r, c, dr, dc, opponent)
            if total == 3 and opens >= 2:  # 活三
                is_open_three = True
                three_count += 1
        board[r][c] = 0
        if is_open_three:
            # 优先防守能形成多个活三的点
            score = three_count * 1000
            # 额外奖励：如果这个位置也能形成自己的活二（挡二成二）
            board[r][c] = my_player
            my_twos = 0
            for dr, dc in DIRECTIONS:
                total, opens = _line_pattern(board, r, c, dr, dc, my_player)
                if total == 2 and opens >= 2:
                    my_twos += 1
            board[r][c] = 0
            score += my_twos * 100
            if score > best_three_score:
                best_three_score = score
                best_three_block = (r, c)
    
    if best_three_block:
        return best_three_block
    
    # 7. 检查对手是否有双冲四或冲四+活三 - 必须防守
    best_block = None
    best_score = 0
    for r, c in get_candidates(board):
        board[r][c] = opponent
        threes, fours = _count_threats(board, r, c, opponent)
        board[r][c] = 0
        # 双冲四或冲四+活三都是致命威胁
        if fours >= 2 or (fours >= 1 and threes >= 1):
            # 优先防守能形成五连的点
            score = fours * 1000 + threes * 100
            if score > best_score:
                best_score = score
                best_block = (r, c)
    
    if best_block:
        return best_block
    
    # 8. 检查十字棋型（四星）- 必须防守
    # 十字棋型：四个棋子分别在上下左右，中间空位形成四星
    cross_block = _find_cross_threat(board, opponent, my_player)
    if cross_block:
        return cross_block
    
    return best_block


def _find_jump_four(board, player):
    """专门检测跳四（被空位隔开的四子）
    例如：XX_XX, X_XXX, XXX_X 等形式
    """
    for r in range(GRID):
        for c in range(GRID):
            if board[r][c] != player:
                continue
                
            for dr, dc in DIRECTIONS:
                # 检查反方向是否有棋子，避免重复
                nr, nc = r - dr, c - dc
                if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                    continue
                
                # 收集这个方向上的所有棋子和空位（最多6个位置）
                pattern = []
                for i in range(6):  # 检查6个位置
                    nr, nc = r + dr * i, c + dc * i
                    if 0 <= nr < GRID and 0 <= nc < GRID:
                        pattern.append((nr, nc, board[nr][nc]))
                    else:
                        break
                
                if len(pattern) < 5:
                    continue
                
                # 检测各种跳四模式
                # 模式1: 11110 (四连+空位)
                # 模式2: 11101 (三连+空位+棋子)
                # 模式3: 11011 (两连+空位+两连)
                # 模式4: 10111 (棋子+空位+三连)
                # 模式5: 01111 (空位+四连)
                
                vals = [p[2] for p in pattern]
                
                # 查找跳四模式
                for i in range(len(vals) - 4):
                    window = vals[i:i+5]
                    player_count = window.count(player)
                    empty_count = window.count(0)
                    
                    # 如果有4个player棋子和1个空位，就是跳四
                    if player_count == 4 and empty_count == 1:
                        # 找到空位位置
                        for j, v in enumerate(window):
                            if v == 0:
                                empty_pos = pattern[i + j]
                                return (empty_pos[0], empty_pos[1])
    
    return None


def _find_cross_threat(board, opponent, my_player):
    """检测十字棋型（四星）威胁
    十字棋型：四个 opponent 棋子分别在 (r-1,c), (r+1,c), (r,c-1), (r,c+1)
    中间 (r,c) 是空位，形成四星威胁
    """
    for r in range(1, GRID - 1):
        for c in range(1, GRID - 1):
            if board[r][c] != 0:
                continue
            
            # 检查上下左右是否都是 opponent 的棋子
            if (board[r-1][c] == opponent and  # 上
                board[r+1][c] == opponent and  # 下
                board[r][c-1] == opponent and  # 左
                board[r][c+1] == opponent):    # 右
                
                # 检查这个四星位置是否形成多个活二威胁
                threat_count = 0
                board[r][c] = opponent
                
                # 检查垂直方向
                total_v, opens_v = _line_pattern(board, r, c, 1, 0, opponent)
                if total_v >= 2 and opens_v >= 2:
                    threat_count += 1
                
                # 检查水平方向
                total_h, opens_h = _line_pattern(board, r, c, 0, 1, opponent)
                if total_h >= 2 and opens_h >= 2:
                    threat_count += 1
                
                board[r][c] = 0
                
                # 如果形成至少两个活二，这是十字威胁，必须防守
                if threat_count >= 2:
                    return (r, c)
    
    return None


def _find_existing_four(board, player):
    """查找当前棋盘上已经存在的四连（冲四或活四）
    返回必须防守的点，如果没有则返回None
    修复版：正确处理所有方向的四子检测
    """
    checked = set()  # 避免重复检测
    
    for r in range(GRID):
        for c in range(GRID):
            if board[r][c] != player:
                continue
                
            for dr, dc in DIRECTIONS:
                # 使用方向标识避免重复
                direction_key = (r, c, dr, dc)
                if direction_key in checked:
                    continue
                    
                # 只朝正方向搜索，避免重复
                # 检查这个方向是否已经被处理过（通过检查反方向的起点）
                nr, nc = r - dr, c - dc
                if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                    continue  # 这不是起点，跳过
                
                # 从当前位置开始，沿正方向数连续棋子
                cells = [(r, c)]
                nr, nc = r + dr, c + dc
                while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                    cells.append((nr, nc))
                    nr += dr
                    nc += dc
                
                # 标记已检查
                for cell in cells:
                    checked.add((cell[0], cell[1], dr, dc))
                
                if len(cells) == 4:
                    # 找到四连，找两端的空位
                    # 一端是当前起点的前一个
                    end1 = (r - dr, c - dc)
                    # 另一端是最后一个棋子的后一个
                    last_r, last_c = cells[-1]
                    end2 = (last_r + dr, last_c + dc)
                    
                    ends = []
                    if 0 <= end1[0] < GRID and 0 <= end1[1] < GRID and board[end1[0]][end1[1]] == 0:
                        ends.append(end1)
                    if 0 <= end2[0] < GRID and 0 <= end2[1] < GRID and board[end2[0]][end2[1]] == 0:
                        ends.append(end2)
                    
                    # 如果只有一个空位，必须防守（冲四）
                    if len(ends) == 1:
                        return ends[0]
                    # 如果有两个空位，这是活四，防守任意一个
                    elif len(ends) == 2:
                        return ends[0]
                
                # 还要检测被空位隔开的四子（跳四）
                if len(cells) == 3:
                    # 检查是否可能是跳四：XX_XX 或 X_XXX 等形式
                    # 在两端或中间找可能的第四子位置
                    last_r, last_c = cells[-1]
                    next_r, next_c = last_r + dr, last_c + dc
                    
                    # 检查延伸方向
                    if 0 <= next_r < GRID and 0 <= next_c < GRID:
                        if board[next_r][next_c] == 0:  # 空位
                            # 检查空位后面是否还有player的棋子
                            nn_r, nn_c = next_r + dr, next_c + dc
                            if 0 <= nn_r < GRID and 0 <= nn_c < GRID and board[nn_r][nn_c] == player:
                                # 这是跳四！必须防守
                                return (next_r, next_c)
    
    return None


def _eval_board_static(board, player):
    """静态评估整盘得分差（player视角）"""
    score = 0
    for r in range(GRID):
        for c in range(GRID):
            if board[r][c] == player:
                for dr, dc in DIRECTIONS:
                    total, opens = _line_pattern(board, r, c, dr, dc, player)
                    score += _score_pattern(total, opens)
            elif board[r][c] == 3 - player:
                for dr, dc in DIRECTIONS:
                    total, opens = _line_pattern(board, r, c, dr, dc, 3 - player)
                    score -= _score_pattern(total, opens)
    return score


class GomokuAI:
    """增强版五子棋AI：置换表 + 杀手启发 + 历史启发 + 迭代加深 + Alpha-Beta"""

    def __init__(self, time_limit=3.0, max_depth=6):
        self.time_limit = time_limit
        self.max_depth = max_depth
        self.MAX = 99999999
        
        # 置换表：缓存已计算的棋局
        self.transposition = {}
        self.tt_hits = 0
        self.tt_size_limit = 50000  # 限制置换表大小
        
        # 杀手启发：记录每层的好走法
        self.killer_moves = {}  # depth -> [(r1,c1), (r2,c2)]
        
        # 历史启发：记录走法的历史成功率
        self.history_table = {}  # (player, r, c) -> score
        self.history_max = 10000  # 历史表分数上限
        
        # 节点计数
        self.nodes_searched = 0
        self.start_time = 0

    def _board_hash(self, board):
        """生成棋盘哈希值（用于置换表）"""
        # 使用简单的字符串哈希
        h = []
        for row in board:
            h.append(''.join(map(str, row)))
        return hash('\n'.join(h)) & 0x7FFFFFFF

    def _is_timeout(self):
        """检查是否超时"""
        return time.time() - self.start_time > self.time_limit

    def best_move(self, board, player):
        """主搜索函数 - 使用迭代加深"""
        self.start_time = time.time()
        self.nodes_searched = 0
        self.tt_hits = 0
        
        # 清空本轮搜索的缓存
        self.killer_moves = {}
        
        opponent = 3 - player

        # 1. 立即赢
        win = find_immediate_win(board, player)
        if win:
            return win

        # 2. 必须防
        block = find_immediate_block(board, player)
        if block:
            return block

        cands = get_candidates(board)
        if not cands:
            return (GRID // 2, GRID // 2)

        # 3. 检查是否能通过强制序列赢棋（深度优先）
        forcing = self._find_forcing_win(board, player, cands, depth=5)
        if forcing:
            return forcing

        # 4. 检查对手强制序列（必须防）
        opp_forcing = self._find_forcing_win(board, opponent, cands, depth=5)
        if opp_forcing:
            return opp_forcing

        # 5. 迭代加深搜索
        best_move = None
        best_score = -self.MAX
        
        # 初始走法排序
        scored = []
        for r, c in cands:
            s = self._quick_score(board, r, c, player)
            scored.append((s, r, c))
        scored.sort(reverse=True)
        
        # 迭代加深：从浅到深搜索
        for depth in range(2, self.max_depth + 1):
            if self._is_timeout():
                break
                
            current_best = None
            current_best_score = -self.MAX
            
            # 使用上一次的排序结果
            moves_to_search = scored[:min(15, len(scored))]
            
            for score, r, c in moves_to_search:
                if self._is_timeout():
                    break
                    
                board[r][c] = player
                if check_win(board, r, c, player):
                    board[r][c] = 0
                    return (r, c)
                    
                # Alpha-Beta搜索
                val = -self._ab_search(board, opponent, player, depth - 1, 
                                       -self.MAX, -current_best_score, depth)
                board[r][c] = 0
                
                if val > current_best_score:
                    current_best_score = val
                    current_best = (r, c)
            
            # 更新最佳结果
            if current_best and not self._is_timeout():
                best_move = current_best
                best_score = current_best_score
                # 按分数重新排序，下次搜索更快
                scored = self._rescore_moves(board, scored, player, depth)

        # 清理置换表（防止内存溢出）
        if len(self.transposition) > self.tt_size_limit:
            self._cleanup_transposition()

        return best_move if best_move else scored[0][1:3]

    def _rescore_moves(self, board, scored, player, depth):
        """根据搜索结果重新排序走法"""
        new_scored = []
        for _, r, c in scored:
            # 检查置换表
            board[r][c] = player
            h = self._board_hash(board)
            board[r][c] = 0
            
            if h in self.transposition:
                entry = self.transposition[h]
                if entry['depth'] >= depth - 1:
                    new_scored.append((entry['score'], r, c))
                    continue
            
            # 使用快速评分
            s = self._quick_score(board, r, c, player)
            new_scored.append((s, r, c))
        
        new_scored.sort(reverse=True)
        return new_scored

    def _ab_search(self, board, cur, root, depth, alpha, beta, ply=0):
        """增强版Alpha-Beta搜索"""
        self.nodes_searched += 1
        opponent = 3 - cur
        
        # 超时检查
        if self._is_timeout():
            return self._eval_board(board, root)
        
        # 检查置换表
        board_hash = self._board_hash(board)
        if board_hash in self.transposition:
            entry = self.transposition[board_hash]
            if entry['depth'] >= depth:
                self.tt_hits += 1
                if entry['flag'] == 'exact':
                    return entry['score']
                elif entry['flag'] == 'lower' and entry['score'] >= beta:
                    return entry['score']
                elif entry['flag'] == 'upper' and entry['score'] <= alpha:
                    return entry['score']

        # 终止条件
        if depth == 0:
            score = self._eval_board(board, root)
            self._store_transposition(board_hash, depth, score, 'exact', alpha, beta)
            return score

        cands = get_candidates(board)
        if not cands:
            return 0

        # 走法排序（关键优化）
        moves = self._order_moves_enhanced(board, cands, cur, depth, ply)
        if not moves:
            return 0

        best_score = -self.MAX
        orig_alpha = alpha

        for i, (_, r, c) in enumerate(moves):
            board[r][c] = cur
            
            # 检查是否赢棋
            if check_win(board, r, c, cur):
                board[r][c] = 0
                score = self.MAX + depth
                self._store_transposition(board_hash, depth, score, 'exact', orig_alpha, beta)
                return score
            
            # PVS (Principal Variation Search)
            if i == 0:
                # 第一个走法完整搜索
                val = -self._ab_search(board, opponent, root, depth - 1, -beta, -alpha, ply + 1)
            else:
                # 后续走法先进行零窗口搜索
                val = -self._ab_search(board, opponent, root, depth - 1, -alpha - 1, -alpha, ply + 1)
                if val > alpha and val < beta:
                    # 重新完整搜索
                    val = -self._ab_search(board, opponent, root, depth - 1, -beta, -alpha, ply + 1)
            
            board[r][c] = 0
            
            if val > best_score:
                best_score = val
                
            if val > alpha:
                alpha = val
                # 更新杀手走法
                self._update_killer(r, c, ply, val)
                # 更新历史表
                self._update_history(cur, r, c, depth)
                
            if alpha >= beta:
                # Beta截断
                break

        # 存储到置换表
        flag = 'exact' if alpha > orig_alpha and alpha < beta else ('lower' if alpha >= beta else 'upper')
        self._store_transposition(board_hash, depth, best_score, flag, orig_alpha, beta)
        
        return best_score

    def _order_moves_enhanced(self, board, cands, cur, depth, ply):
        """增强版走法排序"""
        opp = 3 - cur
        scored = []
        
        # 1. 检查必胜/必防走法
        for r, c in cands[:20]:
            # 检查是否能赢
            board[r][c] = cur
            if check_win(board, r, c, cur):
                board[r][c] = 0
                return [(self.MAX + 1000, r, c)]
            board[r][c] = 0
            
            # 检查对手是否下一步能赢
            board[r][c] = opp
            if check_win(board, r, c, opp):
                board[r][c] = 0
                scored.append((self.MAX - 1, r, c))
                continue
            board[r][c] = 0
        
        if scored:
            return scored[:1]
        
        # 2. 评估普通走法
        for r, c in cands[:20]:
            score = 0
            
            # 杀手走法加分
            if ply in self.killer_moves:
                if (r, c) in self.killer_moves[ply]:
                    score += 5000
            
            # 历史表加分
            hist_key = (cur, r, c)
            if hist_key in self.history_table:
                score += min(self.history_table[hist_key] // 10, 1000)
            
            # 进攻价值
            board[r][c] = cur
            my_threats = 0
            my_fours = 0
            my_open_fours = 0
            for dr, dc in DIRECTIONS:
                t, o = _line_pattern(board, r, c, dr, dc, cur)
                if t >= 5:
                    my_threats += 1000
                elif t == 4:
                    if o >= 2:
                        my_open_fours += 1
                    else:
                        my_fours += 1
                elif t == 3 and o >= 2:
                    my_threats += 50
                elif t == 3 and o == 1:
                    my_threats += 10
            board[r][c] = 0
            
            # 防守价值
            board[r][c] = opp
            opp_threats = 0
            opp_fours = 0
            opp_open_fours = 0
            for dr, dc in DIRECTIONS:
                t, o = _line_pattern(board, r, c, dr, dc, opp)
                if t >= 5:
                    opp_threats += 1000
                elif t == 4:
                    if o >= 2:
                        opp_open_fours += 1
                    else:
                        opp_fours += 1
                elif t == 3 and o >= 2:
                    opp_threats += 50
                elif t == 3 and o == 1:
                    opp_threats += 10
            board[r][c] = 0
            
            # 综合评分
            score += my_open_fours * 10000 + my_fours * 1000 + my_threats * 10
            score += opp_open_fours * 8000 + opp_fours * 800 + opp_threats * 8
            
            # 连接性和位置价值
            conn = 0
            for dr, dc in DIRECTIONS:
                for s in (1, -1):
                    nr, nc = r + dr * s, c + dc * s
                    if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == cur:
                        conn += 1
            center = GRID // 2
            pos = max(0, 7 - abs(r - center) - abs(c - center))
            
            score += conn * 5 + pos * 2
            scored.append((score, r, c))

        scored.sort(reverse=True)
        return scored[:12]

    def _update_killer(self, r, c, ply, score):
        """更新杀手走法"""
        if ply not in self.killer_moves:
            self.killer_moves[ply] = []
        
        # 只保留分数高的走法
        if len(self.killer_moves[ply]) < 2:
            if (r, c) not in self.killer_moves[ply]:
                self.killer_moves[ply].append((r, c))
        elif score > self.MAX // 2:
            # 高分走法替换第一个
            self.killer_moves[ply] = [(r, c), self.killer_moves[ply][0]]

    def _update_history(self, player, r, c, depth):
        """更新历史表"""
        key = (player, r, c)
        if key not in self.history_table:
            self.history_table[key] = 0
        # 深度越深，加分越多
        self.history_table[key] += depth * depth
        # 限制上限
        if self.history_table[key] > self.history_max:
            self.history_table[key] = self.history_max

    def _store_transposition(self, board_hash, depth, score, flag, alpha, beta):
        """存储到置换表"""
        self.transposition[board_hash] = {
            'depth': depth,
            'score': score,
            'flag': flag
        }

    def _cleanup_transposition(self):
        """清理置换表"""
        # 保留最近的一半
        items = list(self.transposition.items())
        self.transposition = dict(items[len(items)//2:])

    def _find_forcing_win(self, board, player, cands, depth=5):
        """搜索强制赢棋序列（增强版VCT/VCF搜索）"""
        opponent = 3 - player
        if depth <= 0:
            return None
            
        for r, c in cands:
            board[r][c] = player
            my_threat_type = self._get_threat_type(board, r, c, player)
            
            if my_threat_type == "open_four":
                board[r][c] = 0
                return (r, c)
            if my_threat_type == "five":
                board[r][c] = 0
                return (r, c)
            if my_threat_type == "four":
                block_pts = self._find_all_four_blocks(board, r, c, player)
                if not block_pts:
                    board[r][c] = 0
                    return (r, c)
                
                all_blocked = True
                for block_pt in block_pts:
                    board[block_pt[0]][block_pt[1]] = opponent
                    
                    cands2 = get_candidates(board)
                    has_follow_up = False
                    
                    for r2, c2 in cands2:
                        board[r2][c2] = player
                        if check_win(board, r2, c2, player):
                            board[r2][c2] = 0
                            board[block_pt[0]][block_pt[1]] = 0
                            board[r][c] = 0
                            return (r, c)
                        
                        t2 = self._get_threat_type(board, r2, c2, player)
                        if t2 == "open_four":
                            board[r2][c2] = 0
                            board[block_pt[0]][block_pt[1]] = 0
                            board[r][c] = 0
                            return (r, c)
                        
                        if depth > 1:
                            next_cands = get_candidates(board)
                            forcing = self._find_forcing_win(board, player, next_cands, depth - 1)
                            if forcing:
                                has_follow_up = True
                        
                        board[r2][c2] = 0
                    
                    board[block_pt[0]][block_pt[1]] = 0
                    
                    if not has_follow_up:
                        all_blocked = False
                        break
                
                if all_blocked:
                    board[r][c] = 0
                    return (r, c)
                    
            board[r][c] = 0
        return None
    
    def _find_all_four_blocks(self, board, r, c, player):
        """找冲四的所有防点"""
        blocks = set()
        for dr, dc in DIRECTIONS:
            total, opens = _line_pattern(board, r, c, dr, dc, player)
            if total == 4 and opens == 1:
                for s in (1, -1):
                    nr, nc = r + dr * s, c + dc * s
                    while 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                        nr += dr * s
                        nc += dc * s
                    if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == 0:
                        blocks.add((nr, nc))
        return list(blocks)

    def _get_threat_type(self, board, r, c, player):
        """判断威胁类型"""
        fours = 0
        open_fours = 0
        for dr, dc in DIRECTIONS:
            total, opens = _line_pattern(board, r, c, dr, dc, player)
            if total >= 5:
                return "five"
            elif total == 4:
                if opens >= 2:
                    open_fours += 1
                elif opens == 1:
                    fours += 1
        if open_fours >= 1:
            return "open_four"
        if fours >= 1:
            return "four"
        return "none"

    def _quick_score(self, board, r, c, player):
        """快速单点评分"""
        opp = 3 - player
        score = 0
        board[r][c] = player
        for dr, dc in DIRECTIONS:
            t, o = _line_pattern(board, r, c, dr, dc, player)
            score += _score_pattern(t, o) * 2
        board[r][c] = 0

        board[r][c] = opp
        for dr, dc in DIRECTIONS:
            t, o = _line_pattern(board, r, c, dr, dc, opp)
            score += _score_pattern(t, o) * 1.5
        board[r][c] = 0

        for dr, dc in DIRECTIONS:
            for s in (1, -1):
                nr, nc = r + dr * s, c + dc * s
                if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                    score += 10
        center = GRID // 2
        score += max(0, 7 - abs(r - center) - abs(c - center))
        return score

    def _eval_board(self, board, player):
        """全局评估函数"""
        my_score = 0
        opp_score = 0
        opp = 3 - player
        
        for r in range(GRID):
            for c in range(GRID):
                if board[r][c] == player:
                    for dr, dc in DIRECTIONS:
                        t, o = _line_pattern(board, r, c, dr, dc, player)
                        my_score += _score_pattern(t, o)
                elif board[r][c] == opp:
                    for dr, dc in DIRECTIONS:
                        t, o = _line_pattern(board, r, c, dr, dc, opp)
                        opp_score += _score_pattern(t, o)
        
        # 连通性评估
        my_conn = 0
        opp_conn = 0
        for r in range(GRID):
            for c in range(GRID):
                if board[r][c] == player:
                    for dr, dc in DIRECTIONS:
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == player:
                            my_conn += 1
                elif board[r][c] == opp:
                    for dr, dc in DIRECTIONS:
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < GRID and 0 <= nc < GRID and board[nr][nc] == opp:
                            opp_conn += 1
        
        return (my_score * 1.3 + my_conn * 5) - (opp_score * 1.0 + opp_conn * 4)


# 全局 AI 引擎实例
_gomoku_ai = GomokuAI(time_limit=2.0)


def cleanup_room(rid, sid):
    """清理房间，避免重复处理"""
    if rid not in rooms:
        return
    room = rooms[rid]
    if sid in room["players"]:
        room["players"].remove(sid)
        room["over"] = True
        emit("opponent_left", room=rid)
    if not room["players"]:
        del rooms[rid]
        print(f"[CLEANUP] 房间 {rid} 已删除")


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("create_room")
def on_create(data=None):
    sid = request.sid
    name = ""
    if data and isinstance(data, dict):
        name = data.get("name", "")[:12]
    # 先清理该玩家可能存在的旧房间
    for rid, room in list(rooms.items()):
        if sid in room["players"]:
            del rooms[rid]
            print(f"[CREATE] 清理旧房间 {rid}")

    rid = gen_room_id()
    ai_enabled = name.lower() == "st"
    rooms[rid] = {
        "board": new_board(),
        "players": [sid],
        "names": {sid: name},
        "turn": 1,
        "over": False,
        "created": time.time(),
        "moves": [],
        "roles": {},  # sid -> player_number
        "ai_enabled": ai_enabled,  # 隐藏AI功能
        "ai_on": False,  # AI开关状态
        "ai_player": 0,  # AI 代表哪个玩家编号
    }
    rooms[rid]["roles"][sid] = 1  # 创建者暂定为1，等加入后随机
    join_room(rid)
    print(f"[CREATE] 房间 {rid} 创建成功, 创建者: {sid}, AI:{ai_enabled}")
    emit("room_created", {"room_id": rid, "player": 1, "ai_enabled": ai_enabled})
    emit("waiting", room=rid)


@socketio.on("set_name")
def on_set_name(data):
    rid = data.get("room_id", "")
    name = data.get("name", "")[:12]
    if rid in rooms:
        rooms[rid]["names"][request.sid] = name


@socketio.on("join_room")
def on_join(data):
    rid = data.get("room_id", "")
    sid = request.sid

    print(f"[JOIN] 收到加入请求 房间:{rid} 加入者:{sid}")

    if rid not in rooms:
        print(f"[JOIN] 房间 {rid} 不存在, 当前房间: {list(rooms.keys())}")
        emit("error_msg", {"msg": "房间不存在"})
        return
    room = rooms[rid]
    if len(room["players"]) >= 2:
        emit("error_msg", {"msg": "房间已满"})
        return
    if room["over"]:
        emit("error_msg", {"msg": "游戏已结束"})
        return

    room["players"].append(sid)
    room["names"][sid] = ""
    join_room(rid)

    p1_sid = room["players"][0]
    p2_sid = sid
    p1_name = room["names"].get(p1_sid, "")
    p2_name = room["names"].get(sid, "")

    # 随机分配先手（每次都用新的随机）
    order = random.choice([(1, 2), (2, 1)])
    p1_role, p2_role = order
    room["roles"][p1_sid] = p1_role
    room["roles"][p2_sid] = p2_role
    room["turn"] = 1  # 黑方（player 1）先走

    black_name = "黑方"
    if p1_role == 1:
        black_sid = p1_sid
    else:
        black_sid = p2_sid

    print(f"[JOIN] 房间 {rid} 匹配完成!")
    print(f"  创建者({p1_sid[:8]}): 执{'黑●' if p1_role == 1 else '白○'}")
    print(f"  加入者({p2_sid[:8]}): 执{'黑●' if p2_role == 1 else '白○'}")
    print(f"  先手: 黑方({black_sid[:8]})")

    # AI 设置：AI 代表创建者（p1）的棋子
    if room.get("ai_enabled"):
        room["ai_player"] = p1_role
        print(f"[AI] 房间 {rid} AI将扮演玩家{p1_role}(创建者)")

    # 先通知双方加入成功
    emit("room_joined", {"room_id": rid, "player": p2_role})
    # 再发送 game_start
    emit(
        "game_start",
        {
            "your_player": p1_role,
            "opponent": p2_name or "对手",
            "ai_enabled": room.get("ai_enabled", False),
            "ai_player": room.get("ai_player", 0),
            "taunt_available": room.get("ai_enabled", False),
        },
        to=p1_sid,
    )
    emit("game_start", {"your_player": p2_role, "opponent": p1_name or "对手"}, to=sid)

    # AI 先手自动落子
    if (
        room.get("ai_on")
        and room.get("ai_enabled")
        and room["turn"] == room.get("ai_player", 0)
    ):
        eventlet.spawn(_execute_ai_move, rid)


@socketio.on("move")
def on_move(data):
    rid = data.get("room_id", "")
    row = data.get("row", -1)
    col = data.get("col", -1)
    sid = request.sid

    if rid not in rooms:
        return
    room = rooms[rid]

    if room["over"] or not (0 <= row < GRID and 0 <= col < GRID):
        return
    if room["board"][row][col] != 0:
        return

    # 使用 roles 字典获取玩家编号
    idx = room["roles"].get(sid, -1)
    if idx == -1 or idx != room["turn"]:
        return

    room["board"][row][col] = idx
    room["moves"].append({"r": row, "c": col, "p": idx})
    win = check_win(room["board"], row, col, idx)
    full = is_full(room["board"])

    result = None
    if win:
        room["over"] = True
        wc = find_win_cells(room["board"], idx)
        result = {"winner": idx, "win_cells": wc}
    elif full:
        room["over"] = True
        result = {"winner": 0}

    next_turn = 3 - idx if not room["over"] else 0
    room["turn"] = next_turn

    print(f"[MOVE] 房间 {rid} 玩家{idx} 落子 ({row},{col})")

    # 分别发给每个玩家
    for pid in room["players"]:
        emit(
            "opponent_move",
            {
                "row": row,
                "col": col,
                "player": idx,
                "next_turn": next_turn,
                "result": result,
            },
            to=pid,
        )

    # AI 自动落子逻辑
    if (
        not room["over"]
        and room.get("ai_on")
        and room.get("ai_enabled")
        and next_turn == room.get("ai_player", 0)
    ):
        eventlet.spawn(_execute_ai_move, rid)


def _execute_ai_move(rid):
    """在 greenthread 中执行 AI 落子（模拟人类思考时间）"""
    # 初始短延迟让客户端同步
    eventlet.sleep(0.3)
    if rid not in rooms:
        return
    room = rooms[rid]
    if room["over"] or not room.get("ai_on") or not room.get("ai_enabled"):
        return
    if room.get("ai_player", 0) != room.get("turn", 0):
        return

    ai_player = room["ai_player"]
    board = room["board"]
    move_count = len(room["moves"])

    # 根据棋局复杂度计算思考时间
    # 开局几步较快，中盘复杂时较慢
    cands = get_candidates(board)
    num_cands = len(cands)

    # 检测是否是简单局面（能赢/能防）
    is_win = find_immediate_win(board, ai_player) is not None
    is_block = find_immediate_block(board, ai_player) is not None
    
    # 检测对手威胁程度
    opponent = 3 - ai_player
    opp_threat_level = 0
    for r, c in cands[:10]:
        board[r][c] = opponent
        threes, fours = _count_threats(board, r, c, opponent)
        board[r][c] = 0
        if fours > 0:
            opp_threat_level = max(opp_threat_level, 3)  # 极高威胁
        elif threes >= 2:
            opp_threat_level = max(opp_threat_level, 2)  # 高威胁
        elif threes == 1:
            opp_threat_level = max(opp_threat_level, 1)  # 中等威胁

    # 根据局面复杂度计算思考时间范围
    if is_win:
        # 能赢的棋 - 快速落子但不要太快（显得从容）
        think_min, think_max = 0.8, 1.5
    elif is_block and opp_threat_level >= 3:
        # 必须防守的紧急情况 - 稍微快一点
        think_min, think_max = 0.6, 1.2
    elif move_count < 4:
        # 开局前几步 - 快速落子
        think_min, think_max = 0.5, 1.0
    elif move_count < 8:
        # 开局中期 - 正常思考
        think_min, think_max = 1.0, 2.0
    elif opp_threat_level >= 2 or num_cands > 35:
        # 复杂局面或对手威胁大 - 深度思考
        think_min, think_max = 2.0, 4.0
    elif num_cands > 25:
        # 中盘复杂
        think_min, think_max = 1.5, 3.0
    else:
        # 普通局面
        think_min, think_max = 1.0, 2.5

    # 添加随机波动，使思考时间更自然
    base_time = random.uniform(think_min, think_max)
    # 添加小幅随机波动（±15%）
    variation = random.uniform(0.85, 1.15)
    think_time = base_time * variation
    
    # 确保思考时间在合理范围内
    think_time = max(0.3, min(think_time, 5.0))
    
    # 记录思考时间用于调试
    print(f"[AI] 思考时间: {think_time:.1f}s (局面: 步数={move_count}, 候选={num_cands}, 威胁={opp_threat_level})")

    # 通知创建者 AI 正在思考（对方不可见）
    creator_sid = room["players"][0] if room["players"] else None
    if creator_sid:
        socketio.emit("ai_thinking", {"thinking": True}, to=creator_sid)

    # 思考中...
    eventlet.sleep(think_time)

    # 重新检查房间状态
    if rid not in rooms:
        if creator_sid:
            socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)
        return
    room = rooms[rid]
    if room["over"] or not room.get("ai_on") or not room.get("ai_enabled"):
        if creator_sid:
            socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)
        return
    if room.get("ai_player", 0) != room.get("turn", 0):
        if creator_sid:
            socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)
        return

    # 计算最佳落子
    move = _gomoku_ai.best_move(board, ai_player)
    if move is None:
        if creator_sid:
            socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)
        return
    r, c = move

    # 再次检查房间状态
    if rid not in rooms:
        return
    room = rooms[rid]
    if room["over"] or room["board"][r][c] != 0:
        if creator_sid:
            socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)
        return
    if room.get("turn") != ai_player:
        if creator_sid:
            socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)
        return

    # 停止思考状态
    if creator_sid:
        socketio.emit("ai_thinking", {"thinking": False}, to=creator_sid)

    room["board"][r][c] = ai_player
    room["moves"].append({"r": r, "c": c, "p": ai_player})
    win = check_win(room["board"], r, c, ai_player)
    full = is_full(room["board"])

    result = None
    if win:
        room["over"] = True
        wc = find_win_cells(room["board"], ai_player)
        result = {"winner": ai_player, "win_cells": wc}
    elif full:
        room["over"] = True
        result = {"winner": 0}

    next_turn = 3 - ai_player if not room["over"] else 0
    room["turn"] = next_turn

    print(f"[AI] 房间 {rid} AI(玩家{ai_player}) 落子 ({r},{c}) 思考{think_time:.1f}s")

    for pid in room["players"]:
        socketio.emit(
            "opponent_move",
            {
                "row": r,
                "col": c,
                "player": ai_player,
                "next_turn": next_turn,
                "result": result,
            },
            to=pid,
        )


@socketio.on("send_taunt")
def on_send_taunt(data):
    """创建者点击按钮，发送一条嘲讽给对手"""
    rid = data.get("room_id", "")
    sid = request.sid
    if rid not in rooms:
        return
    room = rooms[rid]
    if not room.get("ai_enabled"):
        return
    if sid != room["players"][0]:  # 只有创建者可以发送
        return
    if not room["moves"]:  # 没有落子记录无法嘲讽
        return

    # 基于最后一步棋生成嘲讽
    last_move = room["moves"][-1]
    print(f"[TAUNT] 房间 {rid} 玩家{last_move['p']} 落子 ({last_move['r']},{last_move['c']})")
    commentary = get_commentary(
        room["board"],
        (last_move["r"], last_move["c"]),
        last_move["p"],
        len(room["moves"]),
    )
    print(f"[TAUNT] 生成结果: {commentary}")

    if commentary:
        # 发给所有玩家（包括发送者自己，让发送者知道发了什么）
        for pid in room["players"]:
            print(f"[TAUNT] 发送给 {pid[:8]}")
            emit("taunt", commentary, to=pid)


@socketio.on("toggle_ai")
def on_toggle_ai(data):
    rid = data.get("room_id", "")
    sid = request.sid
    if rid not in rooms:
        return
    room = rooms[rid]
    if not room.get("ai_enabled"):
        return
    if sid != room["players"][0]:  # 只有创建者可以切换
        return

    room["ai_on"] = not room.get("ai_on", False)
    print(f"[AI] 房间 {rid} AI开关: {'开启' if room['ai_on'] else '关闭'}")
    emit("ai_status", {"ai_on": room["ai_on"]}, to=sid)

    # 如果刚开启 AI 且当前是 AI 的回合，立即执行
    if (
        room["ai_on"]
        and not room["over"]
        and room.get("turn") == room.get("ai_player", 0)
    ):
        eventlet.spawn(_execute_ai_move, rid)


@socketio.on("restart")
def on_restart(data):
    rid = data.get("room_id", "")
    if rid not in rooms:
        return
    room = rooms[rid]
    if len(room["players"]) < 2:
        return
    room["board"] = new_board()
    room["over"] = False
    room["moves"] = []
    # 重置 AI 开关状态
    room["ai_on"] = False
    # 重新随机分配先手
    p1_sid, p2_sid = room["players"][0], room["players"][1]
    order = random.choice([(1, 2), (2, 1)])
    room["roles"][p1_sid] = order[0]
    room["roles"][p2_sid] = order[1]
    room["turn"] = 1
    # AI 玩家编号跟随创建者新角色
    if room.get("ai_enabled"):
        room["ai_player"] = order[0]
    print(f"[RESTART] 房间 {rid} 重新开始, 新分配: {order}")
    emit("game_restarted", room=rid)
    emit(
        "game_start",
        {
            "your_player": order[0],
            "opponent": room["names"].get(p2_sid, ""),
            "ai_enabled": room.get("ai_enabled", False),
            "ai_player": room.get("ai_player", 0),
        },
        to=p1_sid,
    )
    emit(
        "game_start",
        {"your_player": order[1], "opponent": room["names"].get(p1_sid, "")},
        to=p2_sid,
    )


@socketio.on("undo_request")
def on_undo_request(data):
    rid = data.get("room_id", "")
    sid = request.sid
    if rid not in rooms:
        return
    room = rooms[rid]
    if room["over"] or not room["moves"]:
        return
    for pid in room["players"]:
        if pid != sid:
            emit("undo_asked", to=pid)
            break


@socketio.on("undo_accept")
def on_undo_accept(data):
    rid = data.get("room_id", "")
    if rid not in rooms:
        return
    room = rooms[rid]
    if room["over"] or not room["moves"]:
        return
    last = room["moves"].pop()
    room["board"][last["r"]][last["c"]] = 0
    room["turn"] = last["p"]
    for pid in room["players"]:
        emit("undo_done", {"r": last["r"], "c": last["c"], "player": last["p"]}, to=pid)


@socketio.on("undo_reject")
def on_undo_reject(data):
    rid = data.get("room_id", "")
    sid = request.sid
    if rid not in rooms:
        return
    room = rooms[rid]
    for pid in room["players"]:
        if pid != sid:
            emit("undo_denied", to=pid)
            break


@socketio.on("leave")
def on_leave(data):
    rid = data.get("room_id", "")
    cleanup_room(rid, request.sid)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    for rid, room in list(rooms.items()):
        if sid in room["players"]:
            cleanup_room(rid, sid)
            break


if __name__ == "__main__":
    port = int(__import__("os").environ.get("PORT", 5000))
    # 使用单worker模式，确保rooms字典在所有连接间共享
    # 对于1核CPU的服务器，单worker性能更好且避免数据不同步问题
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True, 
                 use_reloader=False, log_output=True)
