import outcome_planner
import outcome_compiler
import outcomes
import resource_pool


def test_teaching_and_plot_outcomes_target_the_report_window():
    assert outcome_planner.EXACT_ACTIONS[outcomes.TEACH_ON_BOARD] == "RESULT_WINDOW"
    assert outcome_planner.EXACT_ACTIONS[outcomes.PLOT_ON_BOARD] == "RESULT_WINDOW"
    assert outcomes.TEACH_ON_BOARD not in outcome_planner.ALTERNATIVE_GROUPS


def test_resource_pool_exposes_report_but_not_blackboard():
    assert "display" in resource_pool.DESCRIPTORS
    assert "blackboard" not in resource_pool.DESCRIPTORS
    line = resource_pool.DESCRIPTORS["display"].affordance.lower()
    for feature in ("latex", "table", "diagram", "chart"):
        assert feature in line


def test_explanation_and_plot_are_one_coherent_report_outcome():
    compiled = outcome_compiler.compile(
        "explain gradient descent and plot the loss curve")
    assert len(compiled.outcomes) == 1
    assert compiled.outcomes[0].operation == outcomes.TEACH_ON_BOARD
    assert "plot" in compiled.outcomes[0].required_result
