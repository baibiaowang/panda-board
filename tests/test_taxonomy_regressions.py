import unittest

from app.taxonomy import Taxonomy


class RestructuringKeywordRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taxonomy = Taxonomy()

    def test_senior_management_personnel_qualification_is_not_restructuring(self):
        self.assertNotEqual(
            self.taxonomy.classify("关于高级管理人员任职资格审查意见的公告"),
            "restructuring",
        )

    def test_appointment_of_senior_management_is_personnel_change(self):
        self.assertEqual(
            self.taxonomy.classify("关于聘任高级管理人员的公告"),
            "personnel",
        )

    def test_bankruptcy_administrator_remains_restructuring(self):
        self.assertEqual(
            self.taxonomy.classify("关于指定破产管理人的公告"),
            "restructuring",
        )

    def test_restructuring_administrator_remains_restructuring(self):
        self.assertEqual(
            self.taxonomy.classify("关于重整管理人选任的公告"),
            "restructuring",
        )


if __name__ == "__main__":
    unittest.main()
