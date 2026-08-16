-- ============================================================
-- 生命周期隔离：人接管后冻结分类，两条线井水不犯河水。
--
-- 需求方定性（2026-08-17）：新品创新与老品迭代是两个独立生命周期。
-- PM 接手新品创新后，走的就是新品创新的状态迭代，**永远不会落到老品迭代**。
-- 因此机器不得在人接管后改写它的分类归属。
--
-- 015 曾把 opp_type 移出 locked 白名单，理由是「它已是证据派生的路由字段」。
-- 那个理由只在**无人接管**时成立：机器拥有完全改写权时，按证据重新路由是对的。
-- 一旦有人接管（status 非空且非「考虑中」），分类就不再是机器的派生结论，
-- 而是这条记录所属生命周期的身份，必须冻结。
--
-- 三个字段作为一个整体冻结，不拆开：只冻 opp_type 会让 classify_rule 记着
-- 机器的最新判定、而 opp_type 停在旧值，行内自相矛盾。
-- ============================================================

BEGIN;

DO $$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '016 必须以 voc_admin 执行，当前角色为 %', current_user;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION voc_guard_locked() RETURNS trigger AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM voc_opportunity_manual WHERE opp_id = NEW.opp_id;

  -- 未被人接管（无记录或「考虑中」）=> 机器有完全改写权，含按证据重新分类
  IF st IS NULL OR st = '考虑中' THEN
    RETURN NEW;
  END IF;

  -- locked：语义字段与生命周期归属一律保持原值，仅放行统计类白名单
  NEW.title            := OLD.title;
  NEW.problem_mode     := OLD.problem_mode;
  NEW.desc_phenomenon  := OLD.desc_phenomenon;
  NEW.desc_attribution := OLD.desc_attribution;
  NEW.desc_suggestion  := OLD.desc_suggestion;
  NEW.core_tag         := OLD.core_tag;
  NEW.category         := OLD.category;
  NEW.mode_vec         := OLD.mode_vec;
  -- 生命周期归属：人接管后由人主导，机器不得重路由（见文件头）
  NEW.opp_type             := OLD.opp_type;
  NEW.classification_state := OLD.classification_state;
  NEW.classify_rule        := OLD.classify_rule;
  -- 白名单（保持 NEW 值）：evi_total / evi_ec / evi_social / low_star_rate /
  --   countries / product_names / rep_snippets / rank_score / safety_flag /
  --   safety_evidence_ids / dual_source / weak_evidence / category_set /
  --   last_week / needs_review / backlog / updated_at
  RETURN NEW;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION voc_guard_locked() IS
  'locked 行保护人工接管的语义字段与生命周期归属；统计类字段仍按证据重算。';

DO $$
DECLARE frozen int;
BEGIN
  SELECT count(*) INTO frozen
    FROM pg_proc
   WHERE proname = 'voc_guard_locked'
     AND prosrc LIKE '%NEW.opp_type%'
     AND prosrc LIKE '%NEW.classification_state%'
     AND prosrc LIKE '%NEW.classify_rule%';
  IF frozen <> 1 THEN
    RAISE EXCEPTION '016 自检失败：guard 未同时冻结三个分类字段';
  END IF;
  RAISE NOTICE '016 自检通过：人接管后 opp_type / classification_state / classify_rule 均已冻结';
END $$;

COMMIT;
